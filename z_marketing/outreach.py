"""
Outreach: the flow a contact moves through, the launch checks, and sending approved emails.

The rules behind it (marketing/context.md §3 rule 6): hand-picked named work addresses, at most two cold emails,
a small daily limit, a separate sender domain, an unsubscribe link and postal address in every email, and a person
approves every batch. `send_pending()` only ever sends `OutreachSend` rows a person approved; no job picks contacts.
"""
from __future__ import annotations

import html
import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from django.conf import settings
from django.db import close_old_connections, transaction
from django.db.models import Count, Q
from django.utils import timezone

from z_blog.models import NewsletterSubscriber
from z_notification.models import Notification
from z_notification.services import notify

from .models import Campaign, CampaignEmail, Contact, ContactEvent, OutreachSend, Suppression, sender_address

logger = logging.getLogger("zinwe.marketing")

Status = Contact.Status
Send = OutreachSend.Status

LAST_EMAIL = 12
SENDABLE = [Status.NEW, Status.COLD, Status.OPTED, Status.SIGNED]
SHARED_INBOXES = {"info", "admin", "hello", "contact", "sales", "marketing", "support", "office", "accounts", "team"}
DAILY_LIMITS = (10, 20, 25)
STUCK_AFTER = timedelta(minutes=15)
STOP_AFTER_FAILURES = 3  # in a row: the mailbox is down, stop instead of failing the whole batch
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SIGNED_ONLY = re.compile(r"\[Signed-up contacts only: (.*?)\]\s*", re.S)


class OutreachError(Exception):
    """Refused, with a message for staff."""


def today() -> date:
    return timezone.localdate()


def _log(contact: Contact, text: str, actor: str = "") -> None:
    ContactEvent.objects.create(contact=contact, text=text[:240], actor=actor)


def stage_of(contact: Contact, stages: dict[int, str]) -> str:
    if contact.status in (Status.SUPPRESSED, Status.CLOSED, Status.FINISHED):
        return "Ended"
    if contact.status in (Status.PAUSED, Status.SNOOZED):
        return "On hold"
    return stages.get(contact.next_email or contact.last_email or 1, "Cold")


# --- Contacts ---------------------------------------------------------------------


def blocked(email: str) -> bool:
    """Blocked here, or unsubscribed from the newsletter: either way they said no."""
    return (
        Suppression.objects.filter(email__iexact=email).exists()
        or NewsletterSubscriber.objects.filter(email__iexact=email, status=NewsletterSubscriber.Status.UNSUBSCRIBED).exists()
    )


def import_contacts(campaign: Campaign, text: str, actor: str) -> list[dict[str, str]]:
    """One line per person: first name, firm, email, city. One contact per firm; returns a result per line."""
    results = []
    domains = {e.split("@")[-1] for e in Contact.objects.filter(campaign=campaign).values_list("email", flat=True)}
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",")]
        email = parts[2].lower() if len(parts) > 2 else ""
        if len(parts) < 4 or not _EMAIL.match(email) or not parts[0] or not parts[1]:
            results.append({"line": line.strip(), "result": "bad", "message": "Check the format"})
            continue
        local, domain = email.split("@")
        if blocked(email):
            results.append({"line": email, "result": "bad", "message": "Blocked, skipped"})
        elif domain in domains:
            results.append({"line": email, "result": "warn", "message": "Firm already in the list, skipped"})
        elif local in SHARED_INBOXES:
            results.append({"line": email, "result": "warn", "message": "Shared inbox, find a named person"})
        elif domain.endswith("zinwe.io") or domain.endswith("zinwe.tech"):
            results.append({"line": email, "result": "warn", "message": "Our own address, skipped"})
        else:
            contact = Contact.objects.create(
                campaign=campaign, first_name=parts[0][:80], firm=parts[1][:160], email=email, city=parts[3][:80],
                next_date=today(), added_by=actor,
            )
            _log(contact, "Added by hand", actor)
            domains.add(domain)
            results.append({"line": email, "result": "good", "message": "Added"})
    return results


def _suppress(contact: Contact, reason: str, actor: str) -> None:
    contact.status, contact.next_email, contact.next_date, contact.reason = Status.SUPPRESSED, 0, None, reason
    Suppression.objects.get_or_create(email=contact.email.lower(), defaults={"reason": reason})
    # The same address in another campaign stops too.
    Contact.objects.filter(email__iexact=contact.email).exclude(pk=contact.pk).update(
        status=Status.SUPPRESSED, next_email=0, next_date=None, reason=reason
    )
    _log(contact, reason, actor)


ACTIONS = ("yes", "signed", "installed", "notnow", "question", "resume", "noreply", "no", "bounce", "complaint")


@transaction.atomic
def act(contact: Contact, action: str, actor: str, note: str = "") -> Contact:
    """Record what a contact did. The replies themselves arrive in the hello@zinwe.tech inbox."""
    if action not in ACTIONS:
        raise OutreachError("That action isn't available.")
    if contact.status == Status.SUPPRESSED:
        raise OutreachError("This address is on Do not email and can't be changed.")
    now = today()
    was_cold = contact.status in (Status.NEW, Status.COLD, Status.WAITING)
    if action in ("yes", "notnow", "question", "no") and contact.replied_at is None:
        contact.replied_at = now
    if action == "yes":
        contact.status, contact.opted_in_at = Status.OPTED, now
        contact.proof = (note or contact.proof or 'Reply: "yes"')[:240]
        contact.next_email = max(3, contact.next_email or 0, contact.last_email + 1)
        if contact.installed and contact.next_email < 5:
            contact.next_email = 5
        contact.next_date, contact.reason = now, ""
        _log(contact, "Replied yes", actor)
    elif action == "signed":
        contact.signed_up_at = contact.signed_up_at or now
        if contact.status != Status.OPTED:
            contact.status = Status.SIGNED
        if was_cold:
            contact.next_email, contact.next_date = 3, now
        _log(contact, "Signed up", actor)
    elif action == "installed":
        contact.installed = True
        if 2 < contact.next_email < 5:
            contact.next_email = 5
        _log(contact, "Installed, setup emails skipped", actor)
    elif action == "notnow":
        contact.status, contact.next_email, contact.next_date = Status.SNOOZED, 0, None
        contact.reason = (note or "Not now")[:160]
        _log(contact, "Not now, flow stopped", actor)
    elif action == "question":
        contact.prior = {
            "status": contact.status, "next_email": contact.next_email,
            "next_date": contact.next_date.isoformat() if contact.next_date else "",
        }
        contact.status, contact.next_email, contact.next_date = Status.PAUSED, 0, None
        contact.reason = (note or "Question, reply yourself")[:160]
        _log(contact, "Question received", actor)
    elif action == "resume":
        if not contact.prior:
            raise OutreachError("Only a paused contact can resume.")
        contact.status, contact.next_email = contact.prior["status"], contact.prior["next_email"]
        contact.next_date = now if contact.prior["next_date"] else None
        contact.prior, contact.reason = None, ""
        _log(contact, "Resumed", actor)
    elif action == "noreply":
        contact.status, contact.next_email, contact.next_date = Status.CLOSED, 0, None
        contact.reason = "No reply after Email 2"
        _log(contact, "Closed, no reply", actor)
    elif action == "no":
        _suppress(contact, "Said no or unsubscribed", actor)
    elif action == "bounce":
        _suppress(contact, "Hard bounce", actor)
    elif action == "complaint":
        _suppress(contact, "Spam complaint", actor)
        pause(contact.campaign, "A contact marked your email as spam. Find the cause before sending more.")
    contact.save()
    return contact


def unsubscribe(token: str) -> Contact | None:
    """The link in every email. Safe to repeat."""
    contact = Contact.objects.filter(token=(token or "").strip()).first()
    if contact is None:
        return None
    if contact.status != Status.SUPPRESSED:
        with transaction.atomic():
            if contact.replied_at is None:
                contact.replied_at = today()
            _suppress(contact, "Unsubscribed", "")
            contact.save()
    return contact


def after_send(contact: Contact, n: int, steps: dict[int, CampaignEmail]) -> None:
    """Move a contact on after email `n` went out."""
    contact.last_email = n
    _log(contact, f"Email {n} sent")
    if n == 1:
        contact.status, contact.next_email, contact.next_date = Status.COLD, 2, today() + timedelta(days=4)
    elif n == 2:
        contact.status, contact.next_email, contact.next_date = Status.WAITING, 0, None
    elif contact.status == Status.SIGNED and n >= 5:
        contact.status, contact.next_email, contact.next_date = Status.CLOSED, 0, None
        contact.reason = 'Signed up, no "yes" for tips'
    elif n == LAST_EMAIL:
        contact.status, contact.next_email, contact.next_date = Status.FINISHED, 0, None
        contact.reason = "Flow complete"
    else:
        contact.next_email = n + 1
        gap = (steps[n + 1].week - steps[n].week) * 7 if n + 1 in steps else 7
        contact.next_date = today() + timedelta(days=gap or 7)
    contact.save()


def due(campaign: Campaign) -> list[Contact]:
    """Who has an email waiting for approval: furthest in the flow first, so people who said yes never wait."""
    return list(
        campaign.contacts.filter(status__in=SENDABLE, next_email__gt=0, next_date__lte=today())
        .exclude(sends__status__in=[Send.PENDING, Send.SENDING])
        .order_by("-next_email", "next_date", "id")
    )


def sent_today(campaign: Campaign) -> int:
    return OutreachSend.objects.filter(
        contact__campaign=campaign, status__in=[Send.SENT, Send.PENDING, Send.SENDING],
        approved_at__date=today(),
    ).count()


# --- Launch checks ----------------------------------------------------------------


def mailbox_ready() -> bool:
    return bool(settings.EMAIL_OUTREACH_HOST and settings.EMAIL_OUTREACH_HOST_USER and settings.EMAIL_OUTREACH_HOST_PASSWORD)


def sender_domain() -> str:
    return sender_address().split("@")[-1]


def separate_domain() -> bool:
    domain = sender_domain()
    return "." in domain and not re.search(r"(^|\.)zinwe\.io$", domain)


def checks(campaign: Campaign) -> list[dict[str, Any]]:
    dns = campaign.dns_result or {}
    return [
        {"key": "mailbox", "label": "Connect the outreach mailbox", "done": mailbox_ready()},
        {"key": "domain", "label": "Send from a separate domain, not zinwe.io", "done": separate_domain()},
        {"key": "dns", "label": "SPF, DKIM and DMARC pass", "done": all(dns.get(k) for k in ("spf", "dkim", "dmarc"))},
        {"key": "rules", "label": "Check the cold email rules for the USA and India", "done": bool(campaign.rules_checked_at)},
        {"key": "test", "label": "Send a test email to yourself", "done": bool(campaign.test_sent_at)},
        {"key": "contacts", "label": "Audience has contacts", "done": campaign.contacts.filter(status__in=SENDABLE).exists()},
    ]


def _txt(name: str) -> list[str]:
    """TXT records from public DNS (over HTTPS, so the backend needs no DNS library)."""
    url = "https://dns.google/resolve?" + urllib.parse.urlencode({"name": name, "type": "TXT"})
    with urllib.request.urlopen(url, timeout=8) as response:  # noqa: S310 -- fixed https host
        data = json.load(response)
    return [str(a.get("data", "")).strip('"') for a in data.get("Answer", [])]


def check_dns(campaign: Campaign) -> dict[str, bool]:
    domain = sender_domain()
    try:
        root = _txt(domain)
        result = {
            "spf": any(r.startswith("v=spf1") and settings.ZINWE_OUTREACH_SPF_INCLUDE in r for r in root),
            "dkim": any("v=DKIM1" in r for r in _txt(f"{settings.ZINWE_OUTREACH_DKIM_SELECTOR}._domainkey.{domain}")),
            "dmarc": any(r.startswith("v=DMARC1") for r in _txt(f"_dmarc.{domain}")),
        }
    except OSError as exc:
        raise OutreachError("Couldn't reach public DNS. Try again in a minute.") from exc
    campaign.dns_result, campaign.dns_checked_at = result, timezone.now()
    campaign.save(update_fields=["dns_result", "dns_checked_at"])
    return result


def launch(campaign: Campaign, actor: str) -> Campaign:
    missing = [c["label"] for c in checks(campaign) if not c["done"]]
    if missing:
        raise OutreachError("Finish these first: " + "; ".join(missing) + ".")
    campaign.status, campaign.pause_reason = Campaign.Status.ACTIVE, ""
    campaign.save(update_fields=["status", "pause_reason"])
    return campaign


def pause(campaign: Campaign, reason: str = "") -> Campaign:
    campaign.status, campaign.pause_reason = Campaign.Status.PAUSED, reason[:240]
    campaign.save(update_fields=["status", "pause_reason"])
    return campaign


# --- Sending ----------------------------------------------------------------------


def fill(text: str, contact: Contact) -> str:
    return text.replace("{{FirstName}}", contact.first_name).replace("{{Firm}}", contact.firm)


def _body(step: CampaignEmail, contact: Contact) -> str:
    """The text for this contact. Email 5's signed-up line goes only to people who signed up but never said yes."""
    keep = contact.status == Status.SIGNED
    return _SIGNED_ONLY.sub((lambda m: m.group(1) + "\n\n") if keep else "", fill(step.body, contact))


def _html(text: str) -> str:
    def paragraph(block: str) -> str:
        safe = html.escape(block).replace("\n", "<br>")
        safe = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', safe)
        return f'<p style="margin:0 0 14px;">{safe}</p>'

    return "".join(paragraph(b) for b in re.split(r"\n{2,}", text.strip()))


def _context(step: CampaignEmail, contact: Contact, unsubscribe_url: str) -> dict[str, Any]:
    body = _body(step, contact)
    return {
        "subject": fill(step.subject, contact),
        "body_text": body,
        "body_html": _html(body),
        "unsubscribe_url": unsubscribe_url,
        "postal_address": settings.ZINWE_OUTREACH_POSTAL_ADDRESS,
    }


def _deliver(step: CampaignEmail, contact: Contact, to: str, test: bool = False) -> Notification | None:
    site = settings.ZINWE_APP_URL
    url = f"{site}/api/marketing/unsubscribe/{contact.token}/"
    context = _context(step, contact, url)
    headers = {"List-Unsubscribe": f"<{url}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
    if test:
        context["subject"] = f"[Test] {context['subject']}"
    return notify(
        "email", to, "outreach_email", context, ref=contact, headers=headers, from_email=settings.ZINWE_OUTREACH_FROM,
        reply_to=[settings.ZINWE_OUTREACH_REPLY_TO], fallback=False, retry=False, smtp="outreach",
    )


def send_test(campaign: Campaign, n: int, to: str) -> None:
    """One copy of an email to a staff address, through the outreach mailbox, exactly as a contact would get it."""
    if not mailbox_ready():
        raise OutreachError("Connect the outreach mailbox first.")
    step = campaign.emails.filter(n=n).first()
    if step is None:
        raise OutreachError("That email doesn't exist.")
    sample = Contact(campaign=campaign, first_name="Dana", firm="Mesa Ridge CPAs", email=to, token="test")
    note = _deliver(step, sample, to, test=True)
    if note is None or note.status != Notification.Status.SENT:
        raise OutreachError(f"The test didn't go out: {note.error if note else 'unknown error'}"[:300])
    campaign.test_sent_at = timezone.now()
    campaign.save(update_fields=["test_sent_at"])


def approve(campaign: Campaign, expected: int, actor: str) -> int:
    """A person approves today's batch: rows are created, then sent one by one. `expected` = what they saw."""
    if campaign.status != Campaign.Status.ACTIVE:
        raise OutreachError(campaign.pause_reason or "Launch the campaign before sending.")
    if not mailbox_ready():
        raise OutreachError("Connect the outreach mailbox first.")
    room = max(0, campaign.daily_limit - sent_today(campaign))
    batch = due(campaign)[:room]
    if not batch:
        raise OutreachError("Today's limit is reached." if room == 0 else "No emails are due.")
    if len(batch) != expected:
        raise OutreachError("The list changed. Check it and approve again.")
    now = timezone.now()
    with transaction.atomic():
        for contact in batch:
            OutreachSend.objects.update_or_create(
                contact=contact, n=contact.next_email,
                defaults={"status": Send.PENDING, "approved_by": actor, "approved_at": now, "error": ""},
            )
    transaction.on_commit(lambda: start_background(campaign.pk))
    return len(batch)


@dataclass
class RunResult:
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    stuck: int = 0

    def summary(self) -> str:
        return f"{self.sent} sent, {self.failed} failed, {self.skipped} skipped, {self.stuck} stuck"


def _mark_stuck() -> int:
    return OutreachSend.objects.filter(status=Send.SENDING, claimed_at__lt=timezone.now() - STUCK_AFTER).update(
        status=Send.FAILED, error="Stopped mid-send; not sent again automatically."
    )


def send_pending(campaign_id: int | None = None) -> RunResult:
    """Send what a person approved. Safe to run twice at once: each row is claimed before it goes."""
    result = RunResult(stuck=_mark_stuck())
    rows = OutreachSend.objects.filter(status=Send.PENDING).select_related("contact", "contact__campaign")
    if campaign_id is not None:
        rows = rows.filter(contact__campaign_id=campaign_id)
    failures = 0
    for row in rows.order_by("approved_at", "id"):
        if failures >= STOP_AFTER_FAILURES:
            break
        if not OutreachSend.objects.filter(pk=row.pk, status=Send.PENDING).update(
            status=Send.SENDING, claimed_at=timezone.now()
        ):
            continue
        contact = Contact.objects.select_related("campaign").get(pk=row.contact_id)
        campaign = contact.campaign
        steps = {s.n: s for s in campaign.emails.all()}
        still_due = (
            campaign.status == Campaign.Status.ACTIVE and contact.status in SENDABLE
            and contact.next_email == row.n and row.n in steps and not blocked(contact.email)
        )
        if not still_due:
            OutreachSend.objects.filter(pk=row.pk).update(status=Send.SKIPPED, error="No longer due when its turn came.")
            result.skipped += 1
            continue
        note = _deliver(steps[row.n], contact, contact.email)
        if note is not None and note.status == Notification.Status.SENT:
            OutreachSend.objects.filter(pk=row.pk).update(status=Send.SENT, sent_at=timezone.now())
            after_send(contact, row.n, steps)
            result.sent += 1
            failures = 0
        else:
            OutreachSend.objects.filter(pk=row.pk).update(
                status=Send.FAILED, error=(note.error if note else "Not sent")[:300]
            )
            result.failed += 1
            failures += 1
        time.sleep(settings.ZINWE_OUTREACH_GAP_SECONDS)
    return result


def _send_in_background(campaign_id: int) -> None:
    try:
        send_pending(campaign_id)
    except Exception:  # noqa: BLE001 -- run_jobs picks up what's left
        logger.exception("Outreach send for campaign %s stopped", campaign_id)
    finally:
        close_old_connections()


def start_background(campaign_id: int) -> None:
    threading.Thread(target=_send_in_background, args=(campaign_id,), name=f"outreach-{campaign_id}", daemon=True).start()


# --- Numbers ----------------------------------------------------------------------


def report(campaign: Campaign) -> dict[str, Any]:
    """Real counts only. Opens and clicks are never tracked: pixels hurt cold email and break the privacy promise."""
    contacts = campaign.contacts.all()
    sends = OutreachSend.objects.filter(contact__campaign=campaign, status=Send.SENT)
    emailed = sends.values("contact").distinct().count()
    bounced = contacts.filter(reason="Hard bounce").count()
    unsubscribed = contacts.filter(reason__in=["Unsubscribed", "Said no or unsubscribed"]).count()
    complaints = contacts.filter(reason="Spam complaint").count()
    by_step = dict(sends.values_list("n").annotate(c=Count("id")).order_by())
    rate = lambda n: round(n / emailed * 100, 1) if emailed else 0  # noqa: E731
    return {
        "funnel": [
            {"label": "Emailed", "value": emailed},
            {"label": "Replied", "value": contacts.filter(replied_at__isnull=False).count()},
            {"label": "Said yes", "value": contacts.filter(opted_in_at__isnull=False).count()},
            {"label": "Signed up", "value": contacts.filter(signed_up_at__isnull=False).count()},
            {"label": "Installed", "value": contacts.filter(installed=True).count()},
        ],
        "by_email": [{"n": s.n, "subject": s.subject, "stage": s.stage, "sent": by_step.get(s.n, 0)} for s in campaign.emails.all()],
        "health": {
            "bounce_rate": rate(bounced), "unsubscribe_rate": rate(unsubscribed), "complaints": complaints,
        },
    }


def counts(campaign: Campaign) -> dict[str, int]:
    found = dict(campaign.contacts.values_list("status").annotate(c=Count("id")).order_by())
    return {
        "total": sum(found.values()),
        "due": len(due(campaign)),
        "waiting": found.get(Status.WAITING, 0),
        "paused": found.get(Status.PAUSED, 0),
        "cold": sum(found.get(s, 0) for s in (Status.NEW, Status.COLD, Status.WAITING)),
        "opted": sum(found.get(s, 0) for s in (Status.OPTED, Status.SIGNED)),
        "blocked": found.get(Status.SUPPRESSED, 0),
        "failed": OutreachSend.objects.filter(contact__campaign=campaign, status=Send.FAILED).count(),
        "needs_you": campaign.contacts.filter(Q(status=Status.WAITING) | Q(status=Status.PAUSED)).count(),
    }
