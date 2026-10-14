"""
Blog newsletter: double opt-in sign-up from the website, unsubscribe, and emailing an article to
every confirmed subscriber.

Sending is idempotent: each (issue, subscriber) pair is one `NewsletterDelivery` row, claimed
atomically before its email goes out, so a background thread and the `run_jobs` loop can both run
`send_pending()` without anyone getting the email twice. An issue queued for a *scheduled*
article waits until the article goes live.
"""
from __future__ import annotations

import html as html_lib
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.models import Count, Q
from django.utils import timezone

from z_notification.models import Notification
from z_notification.services import notify

from . import services
from .models import Article, NewsletterDelivery, NewsletterIssue, NewsletterSubscriber
from .services import BlogError

logger = logging.getLogger("zinwe.blog.newsletter")

Sub = NewsletterSubscriber.Status
Issue = NewsletterIssue.Status
Delivery = NewsletterDelivery.Status

# A pending address gets at most one confirmation email an hour, however often the form is sent.
CONFIRM_RESEND_AFTER = timedelta(hours=1)


def _site() -> str:
    return settings.ZINWE_APP_URL


# --- Subscribers ------------------------------------------------------------------


def subscribe(email: str, source: str = "") -> None:
    """Start (or restart) a sign-up and email the confirmation link.

    Says nothing about whether the address was already subscribed (the caller always shows
    "check your inbox"), so the form can't be used to test who's on the list.
    """
    email = email.strip().lower()
    now = timezone.now()
    with transaction.atomic():
        sub, _ = NewsletterSubscriber.objects.select_for_update().get_or_create(email=email)
        if sub.status == Sub.ACTIVE:
            return
        if sub.status == Sub.PENDING and sub.confirm_sent_at and now - sub.confirm_sent_at < CONFIRM_RESEND_AFTER:
            return
        sub.status = Sub.PENDING
        sub.source = (source or "")[:240]
        sub.confirm_sent_at = now
        sub.save(update_fields=["status", "source", "confirm_sent_at"])
    notify(
        "email", email, "newsletter_confirm",
        {"confirm_url": f"{_site()}/newsletter?confirm={sub.token}"},
        ref=sub,
    )


def _by_token(token: str) -> NewsletterSubscriber:
    sub = NewsletterSubscriber.objects.filter(token=(token or "").strip()).first() if token else None
    if sub is None:
        raise BlogError("This link isn't valid. Sign up again on the blog.")
    return sub


def confirm(token: str) -> NewsletterSubscriber:
    sub = _by_token(token)
    if sub.status == Sub.UNSUBSCRIBED:
        raise BlogError("You unsubscribed from this list. Sign up again on the blog to get the emails.")
    if sub.status == Sub.PENDING:
        sub.status = Sub.ACTIVE
        sub.confirmed_at = timezone.now()
        sub.save(update_fields=["status", "confirmed_at"])
    return sub


def unsubscribe(token: str) -> NewsletterSubscriber:
    sub = _by_token(token)
    if sub.status != Sub.UNSUBSCRIBED:
        sub.status = Sub.UNSUBSCRIBED
        sub.unsubscribed_at = timezone.now()
        sub.save(update_fields=["status", "unsubscribed_at"])
    return sub


def counts() -> dict[str, int]:
    found = dict(NewsletterSubscriber.objects.values_list("status").annotate(n=Count("id")).order_by())
    return {key: int(found.get(key, 0)) for key in Sub.values}


def audience() -> int:
    return NewsletterSubscriber.objects.filter(status=Sub.ACTIVE).count()


# --- The email ----------------------------------------------------------------------

# Email clients ignore <style> blocks unevenly, so the article's tags get inline styles.
_INLINE = {
    "h2": "margin:28px 0 10px;font-size:21px;line-height:1.3;color:#111827;",
    "h3": "margin:22px 0 8px;font-size:18px;line-height:1.35;color:#111827;",
    "h4": "margin:18px 0 6px;font-size:16px;color:#111827;",
    "p": "margin:0 0 14px;",
    "a": "color:#c2410c;",
    "img": "display:block;max-width:100%;height:auto;margin:16px 0;border:0;border-radius:6px;",
    "blockquote": "margin:16px 0;padding:2px 0 2px 14px;border-left:3px solid #FD6D02;color:#4b5563;",
    "pre": "margin:16px 0;padding:12px 14px;background:#15161a;color:#f2f2ef;border-radius:6px;font-size:13px;white-space:pre-wrap;",
    "ul": "margin:0 0 14px;padding-left:22px;",
    "ol": "margin:0 0 14px;padding-left:22px;",
}
_TAG = re.compile(r"<(" + "|".join(_INLINE) + r")(?=[\s>/])")
_SITE_URL = re.compile(r'(\s(?:src|href))="(/[^"]*)"')


def email_html(body_html: str) -> str:
    """Article HTML (already allow-list cleaned, no style attributes) made email-ready:
    inline styles, and site-relative links / images made absolute."""
    out = _SITE_URL.sub(lambda m: f'{m[1]}="{_site()}{m[2]}"', body_html)
    return _TAG.sub(lambda m: f'<{m[1]} style="{_INLINE[m[1]]}"', out)


def email_text(body_html: str) -> str:
    """Readable plain-text version: paragraphs, headings and list items on their own lines."""
    text = re.sub(r"<li[^>]*>", "\n- ", body_html)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</(p|h2|h3|h4|blockquote|pre|ul|ol|figure)>", "\n\n", text)
    text = re.sub(r'<img[^>]*alt="([^"]*)"[^>]*>', r"[Image: \1]\n", text)
    text = html_lib.unescape(re.sub(r"<[^>]+>", "", text))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def default_subject(article: Article) -> str:
    return article.title[:200]


def _context(article: Article, subject: str, unsubscribe_url: str) -> dict[str, Any]:
    full = article.type == Article.Type.NEWSLETTER  # a newsletter is read in the inbox; others are teasers
    return {
        "subject": subject,
        "article": article,
        "full": full,
        "body_html": email_html(article.body_html) if full else "",
        "body_text": email_text(article.body_html) if full else "",
        "cover_url": f"{_site()}{article.cover_image.url}" if article.cover_image else "",
        "article_url": f"{_site()}/blog/{article.slug}",
        "blog_url": f"{_site()}/blog",
        "unsubscribe_url": unsubscribe_url,
    }


def _send(article: Article, subject: str, sub: NewsletterSubscriber | None, to: str) -> Notification | None:
    if sub is not None:
        page = f"{_site()}/newsletter?unsubscribe={sub.token}"
        # RFC 8058 one-click unsubscribe: Gmail / Outlook show their own "Unsubscribe" button.
        headers = {
            "List-Unsubscribe": f"<{_site()}/api/blog/newsletter/unsubscribe/{sub.token}/>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }
    else:  # a test send: there's no subscription to cancel
        page, headers = f"{_site()}/blog", None
    return notify("email", to, "newsletter_issue", _context(article, subject, page), ref=article, headers=headers)


def send_test(article: Article, to: str) -> Notification | None:
    """Send one copy to a staff member, exactly as subscribers will get it (subject marked [Test])."""
    if not services.plain_text(article.body_html):
        raise BlogError("Write the article before sending a test.")
    return _send(article, f"[Test] {default_subject(article)}", None, to)


# --- Issues -----------------------------------------------------------------------


def queue(article: Article, subject: str, staff: Any) -> NewsletterIssue:
    """Email this article to every confirmed subscriber: now if it's live, else when it goes live."""
    article_state = services.state(article)
    if article_state == "draft":
        raise BlogError("Publish or schedule the article before emailing it.")
    if audience() == 0:
        raise BlogError("Nobody has confirmed a subscription yet, so there's no one to email.")
    try:
        with transaction.atomic():
            issue = NewsletterIssue.objects.create(
                article=article, subject=(subject or "").strip()[:200] or default_subject(article), queued_by=staff
            )
    except IntegrityError as exc:
        raise BlogError("This article has already been emailed to subscribers.") from exc
    if article_state == "published":
        transaction.on_commit(lambda: start_background(issue.pk))
    return issue


def on_published(article: Article) -> None:
    """An article just went live by hand ("Publish now"): send its queued email straight away
    instead of waiting for the next `run_jobs` round."""
    issue = NewsletterIssue.objects.filter(article=article, status=Issue.QUEUED).first()
    if issue is not None and services.is_live(article):
        transaction.on_commit(lambda: start_background(issue.pk))


def retry_failed(issue: NewsletterIssue) -> int:
    """Put failed deliveries back in the queue and send them again."""
    n = issue.deliveries.filter(status=Delivery.FAILED).update(status=Delivery.PENDING, error="")
    if n:
        NewsletterIssue.objects.filter(pk=issue.pk).update(status=Issue.SENDING, finished_at=None)
        transaction.on_commit(lambda: start_background(issue.pk))
    return n


def progress(issue: NewsletterIssue | None) -> dict[str, Any] | None:
    if issue is None:
        return None
    found = dict(issue.deliveries.values_list("status").annotate(n=Count("id")).order_by())
    total = sum(found.values())
    return {
        "subject": issue.subject,
        "status": issue.status,
        "waiting_for_publish": issue.status == Issue.QUEUED and not services.is_live(issue.article),
        "total": total,
        "sent": int(found.get(Delivery.SENT, 0)),
        "failed": int(found.get(Delivery.FAILED, 0)),
        "skipped": int(found.get(Delivery.SKIPPED, 0)),
        "pending": int(found.get(Delivery.PENDING, 0) + found.get(Delivery.SENDING, 0)),
        "queued_by": getattr(issue.queued_by, "email", None),
        "created_at": issue.created_at,
        "started_at": issue.started_at,
        "finished_at": issue.finished_at,
    }


# --- Sending ----------------------------------------------------------------------


@dataclass
class RunResult:
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    waiting: list[str] = field(default_factory=list)  # issues held until their article is live

    def summary(self) -> str:
        return (
            f"{self.sent} sent, {self.failed} failed, {self.skipped} skipped (unsubscribed), "
            f"{len(self.waiting)} waiting for publish"
        )


def _start(issue: NewsletterIssue, now: datetime) -> None:
    """Snapshot today's confirmed subscribers as the issue's recipients (once)."""
    with transaction.atomic():
        claimed = NewsletterIssue.objects.filter(pk=issue.pk, status=Issue.QUEUED).update(
            status=Issue.SENDING, started_at=now
        )
        if not claimed:
            return  # another sender started it
        NewsletterDelivery.objects.bulk_create(
            [
                NewsletterDelivery(issue=issue, subscriber=s)
                for s in NewsletterSubscriber.objects.filter(status=Sub.ACTIVE).only("id")
            ],
            ignore_conflicts=True,
        )


def send_pending(issue_id: int | None = None, limit: int = 500, now: datetime | None = None) -> RunResult:
    """Send up to `limit` waiting newsletter emails (all issues, or one). Safe to run concurrently."""
    now = now or timezone.now()
    result = RunResult()
    issues = NewsletterIssue.objects.filter(status__in=[Issue.QUEUED, Issue.SENDING]).select_related(
        "article", "article__cover_image"
    ).order_by("created_at")
    if issue_id is not None:
        issues = issues.filter(pk=issue_id)
    budget = limit
    for issue in issues:
        if services.state(issue.article, now) != "published":
            result.waiting.append(issue.subject)
            continue
        if issue.status == Issue.QUEUED:
            _start(issue, now)
        for delivery in issue.deliveries.filter(status=Delivery.PENDING).select_related("subscriber")[:budget]:
            if not NewsletterDelivery.objects.filter(pk=delivery.pk, status=Delivery.PENDING).update(
                status=Delivery.SENDING
            ):
                continue  # another sender has it
            budget -= 1
            sub = delivery.subscriber
            if sub.status != Sub.ACTIVE:
                delivery.status = Delivery.SKIPPED
                result.skipped += 1
            else:
                note = _send(issue.article, issue.subject, sub, sub.email)
                if note is not None and note.status == Notification.Status.SENT:
                    delivery.status, delivery.sent_at = Delivery.SENT, timezone.now()
                    result.sent += 1
                else:
                    delivery.status = Delivery.FAILED
                    delivery.error = (note.error if note is not None else "Not sent")[:300]
                    result.failed += 1
            delivery.save(update_fields=["status", "sent_at", "error"])
        if not issue.deliveries.filter(status__in=[Delivery.PENDING, Delivery.SENDING]).exists():
            NewsletterIssue.objects.filter(pk=issue.pk, status=Issue.SENDING).update(
                status=Issue.SENT, finished_at=timezone.now()
            )
        if budget <= 0:
            break
    return result


def _send_in_background(issue_id: int) -> None:
    try:
        close_old_connections()
        result = send_pending(issue_id=issue_id, limit=10_000)
        logger.info("Newsletter %s: %s", issue_id, result.summary())
    except Exception:  # noqa: BLE001 -- run_jobs picks up whatever is left
        logger.exception("Newsletter %s background send failed", issue_id)
    finally:
        connection.close()


def start_background(issue_id: int) -> None:
    """Send an issue now without holding up the request; `run_jobs` is the safety net."""
    threading.Thread(target=_send_in_background, args=(issue_id,), name=f"newsletter-{issue_id}", daemon=True).start()


def subscriber_rows(status: str = "", q: str = "") -> list[NewsletterSubscriber]:
    qs = NewsletterSubscriber.objects.all()
    if status in Sub.values:
        qs = qs.filter(status=status)
    if q.strip():
        qs = qs.filter(Q(email__icontains=q.strip()) | Q(source__icontains=q.strip()))
    return list(qs[:1000])
