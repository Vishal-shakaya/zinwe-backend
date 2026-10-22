"""
Guide follow-ups: a short sequence of emails after a member's first download of a guide (e.g. day 0 "here's
your guide", day 3 one worksheet, day 7 a story episode, day 12 "see it on your own PCs"). Owner-approved: a guide's
follow-ups go out only when `followups_enabled` is on.

Kind to the inbox: from 09:00 in the member's own time zone, at most one Zinwe email a day — a follow-up waits a
day if the member got a series episode or another follow-up that day. Each (guide, member, step) is one
`GuideFollowupSend` row, claimed before sending, so concurrent runs never send twice. `run_jobs` calls `send_due()`
every hour.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from z_notification.models import Notification
from z_notification.services import notify

from . import guides as gsvc
from .drip import zone
from .models import Guide, GuideDownload, GuideFollowupSend, NewsletterSubscriber, SeriesSend

logger = logging.getLogger("zinwe.blog.guide_followups")
Send = GuideFollowupSend.Status
SEND_HOUR = 9


def steps(guide: Guide) -> list[dict[str, Any]]:
    """The guide's follow-ups, cleaned and ordered by day."""
    out = []
    for raw in guide.followups or []:
        if not isinstance(raw, dict) or not str(raw.get("subject") or "").strip():
            continue
        try:
            day = max(0, min(60, int(raw.get("day", 0))))
        except (TypeError, ValueError):
            continue
        out.append({
            "day": day,
            "subject": str(raw.get("subject")).strip()[:200],
            "preview": str(raw.get("preview") or "").strip()[:200],
            "body_html": str(raw.get("body_html") or ""),
            "cta_label": str(raw.get("cta_label") or "").strip()[:60],
            "cta_url": str(raw.get("cta_url") or "").strip()[:300],
        })
    return sorted(out, key=lambda s: s["day"])


def clean_steps(raw: Any) -> list[dict[str, Any]]:
    """Staff input: keep the shape, clean each body with the article allow-list."""
    from .services import clean_body

    g = Guide(followups=raw if isinstance(raw, list) else [])
    return [{**s, "body_html": clean_body(s["body_html"])} for s in steps(g)][:8]


def _site() -> str:
    return settings.ZINWE_APP_URL


def _link(url: str, guide: Guide, step: int, sub: NewsletterSubscriber | None) -> str:
    """Absolute, tagged with the guide's campaign; site pages also get the member's reader key."""
    if not url:
        return ""
    absolute = url if re.match(r"^https?://", url) else f"{_site()}{url if url.startswith('/') else '/' + url}"
    sep = "&" if "?" in absolute else "?"
    tags = f"utm_source=guide_email&utm_medium=email&utm_campaign={gsvc.campaign(guide)}&utm_content=step{step + 1}"
    key = f"&k={sub.access_key}" if (sub is not None and "/blog/" in absolute and "k=" not in absolute) else ""
    return f"{absolute}{sep}{tags}{key}"


def _context(guide: Guide, step_no: int, step: dict[str, Any], sub: NewsletterSubscriber | None, test: bool) -> dict[str, Any]:
    from .newsletter import email_html, email_text

    return {
        "subject": ("[Test] " if test else "") + step["subject"],
        "preview": step["preview"],
        "guide": guide,
        "cover_url": f"{_site()}{guide.cover_image.url}" if guide.cover_image else "",
        "body_html": email_html(step["body_html"]),
        "body_text": email_text(step["body_html"]),
        "cta_label": step["cta_label"],
        "cta_url": _link(step["cta_url"], guide, step_no, sub),
        "guide_url": _link(gsvc.path(guide), guide, step_no, sub),
        "unsubscribe_url": f"{_site()}/newsletter?unsubscribe={sub.token}" if sub else f"{_site()}/blog/guides",
        "postal_address": getattr(settings, "ZINWE_POSTAL_ADDRESS", "") or "SKYTECHNO, New Delhi, India",
        "step_no": step_no + 1,
        "step_count": len(steps(guide)),
    }


def _send(guide: Guide, step_no: int, sub: NewsletterSubscriber | None, to: str, test: bool = False) -> Notification | None:
    step = steps(guide)[step_no]
    headers = None
    if sub is not None:
        headers = {
            "List-Unsubscribe": f"<{_site()}/api/blog/newsletter/unsubscribe/{sub.token}/>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }
    return notify(
        "email", to, "guide_followup", _context(guide, step_no, step, sub, test), ref=guide, headers=headers, fallback=False,
    )


def send_tests(guide: Guide, to: str) -> int:
    """Every follow-up of a guide to a staff member, marked [Test], for approval."""
    sent = 0
    for i in range(len(steps(guide))):
        note = _send(guide, i, None, to, test=True)
        sent += int(note is not None and note.status == Notification.Status.SENT)
    return sent


@dataclass
class FollowupResult:
    sent: int = 0
    failed: int = 0
    deferred: int = 0  # due, but the member already had a Zinwe email today

    def summary(self) -> str:
        return f"{self.sent} sent, {self.failed} failed, {self.deferred} waiting a day"


def _emailed_today(sub: NewsletterSubscriber, today: date) -> bool:
    return (
        SeriesSend.objects.filter(subscriber=sub, local_date=today).exists()
        or GuideFollowupSend.objects.filter(subscriber=sub, local_date=today).exists()
    )


def send_due(now: datetime | None = None, limit: int = 300) -> FollowupResult:
    now = now or timezone.now()
    result = FollowupResult()
    for guide in Guide.objects.filter(status=Guide.Status.PUBLISHED, followups_enabled=True).select_related("cover_image"):
        plan = steps(guide)
        if not plan:
            continue
        downloads = GuideDownload.objects.filter(
            guide=guide, subscriber__status=NewsletterSubscriber.Status.ACTIVE
        ).select_related("subscriber")
        if guide.followups_enabled_at:  # only downloads made since the follow-ups were approved
            downloads = downloads.filter(first_at__gte=guide.followups_enabled_at)
        done = {}
        for sub_id, step in GuideFollowupSend.objects.filter(guide=guide).exclude(status=Send.FAILED).values_list("subscriber_id", "step"):
            done.setdefault(sub_id, set()).add(step)
        for dl in downloads:
            sub = dl.subscriber
            local = now.astimezone(zone(sub.timezone))
            if local.hour < SEND_HOUR:
                continue
            age = (local.date() - dl.first_at.astimezone(zone(sub.timezone)).date()).days
            due = next((i for i, s in enumerate(plan) if s["day"] <= age and i not in done.get(sub.pk, set())), None)
            if due is None:
                continue
            if _emailed_today(sub, local.date()):
                result.deferred += 1
                continue
            row = _claim(guide, sub, due, local.date())
            if row is None:
                continue
            note = _send(guide, due, sub, sub.email)
            if note is not None and note.status == Notification.Status.SENT:
                row.status, row.sent_at = Send.SENT, timezone.now()
                row.save(update_fields=["status", "sent_at"])
                result.sent += 1
            else:
                row.status, row.error = Send.FAILED, (note.error if note else "Not sent")[:300]
                row.save(update_fields=["status", "error"])
                result.failed += 1
            limit -= 1
            if limit <= 0:
                return result
    return result


def _claim(guide: Guide, sub: NewsletterSubscriber, step: int, today: date) -> GuideFollowupSend | None:
    try:
        with transaction.atomic():
            return GuideFollowupSend.objects.create(guide=guide, subscriber=sub, step=step, local_date=today)
    except IntegrityError:
        pass
    taken = GuideFollowupSend.objects.filter(guide=guide, subscriber=sub, step=step, status=Send.FAILED).update(
        status=Send.SENDING, local_date=today, error=""
    )
    return GuideFollowupSend.objects.get(guide=guide, subscriber=sub, step=step) if taken else None


def stats(guide: Guide) -> list[dict[str, Any]]:
    sent = {}
    for step in GuideFollowupSend.objects.filter(guide=guide, status=Send.SENT).values_list("step", flat=True):
        sent[step] = sent.get(step, 0) + 1
    return [{"day": s["day"], "subject": s["subject"], "sent": sent.get(i, 0)} for i, s in enumerate(steps(guide))]
