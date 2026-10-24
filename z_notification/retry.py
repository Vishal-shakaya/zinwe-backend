"""
Retrying failed transactional emails (FIX-008). `run_jobs` calls `run()` every round.

1. Rows stuck in "queued" for longer than `STUCK_AFTER` (the process died mid-send: a worker killed during the
   SMTP call, a deploy restart) are marked failed. If they kept a retry payload they are retried below.
2. Failed rows that are retryable (`retry_payload` set) are sent again once their `next_retry_at` has come, with
   growing waits (`BACKOFF`) and at most `MAX_ATTEMPTS` sends in all, never later than `GIVE_UP_AFTER`. The same
   row is updated: `attempts`, `error` ("Attempt 3 of 5 failed: ..."), then sent / given up.

What is retryable is decided by `notify()` when it sends (`retryable()`):
- never anything with `redact` values (one-time codes, the member enrol ID): the secret isn't stored, and an expired
  sign-in / verification code is useless and confusing; `otp_*` templates and `z_auth.onetimecode` refs are refused
  even when a caller asks;
- never `fallback=False` mail (newsletter, series, guide follow-ups): bulk mail goes through the primary SMTP only
  and its claim rows (z_blog) retry it;
- not the templates whose callers already handle a failure (`CALLER_RETRIES`): they retry on their own or tell
  someone at once, and a second copy would be a duplicate.

Sending again goes through the normal channel, so the FailoverEmailBackend applies (Brevo, then the Gmail fallback;
a primary failure skips Brevo for EMAIL_FAILOVER_PAUSE_SECONDS in this process). The first wait (10 min) is longer
than that pause. A send that dies after the SMTP server accepted it but before the row is updated can be sent twice:
at-least-once, the safe side for receipts and replies.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

from django.utils import timezone

from .models import Notification

logger = logging.getLogger("zinwe.notification.retry")

STUCK_AFTER = timedelta(minutes=15)  # far longer than any send (two SMTP servers x EMAIL_TIMEOUT)
BACKOFF = (timedelta(minutes=10), timedelta(minutes=30), timedelta(hours=2), timedelta(hours=6))
MAX_ATTEMPTS = len(BACKOFF) + 1  # the first send + one retry after each wait
GIVE_UP_AFTER = timedelta(days=2)  # a two-day-old receipt or reply is still worth it; older is noise
STOP_AFTER_FAILURES = 3  # in a row, in one round: SMTP is down, leave the rest for the next round
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024  # attachments bigger than this aren't kept (not retried)

# Templates whose callers handle a failure themselves (retrying here would send a second copy).
CALLER_RETRIES = frozenset({
    "feedback_request",  # z_auth.feedback_requests: tried again next run
    "renewal_reminder", "plan_expired",  # z_auth.renewals: deletes its claim and tries again next run
    "account_suspended", "admin_message",  # z_adminapi: the staff member is told at once
    "member_alert",  # z_devices: the owner is told at once (EmailNotSent) and can send again
    "report_shared", "report_member",  # z_devices.report_mail: the ReportDelivery records the outcome
})
NEVER_PREFIXES = ("otp_", "newsletter_")
NEVER_REFS = frozenset({"z_auth.onetimecode"})


def retryable(
    channel: str, template: str, ref_type: str, redact: Iterable[str], fallback: bool, retry: bool | None
) -> bool:
    """Whether a failed send of this email may be sent again later (see the module docstring)."""
    if channel != "email" or any(redact) or not fallback:
        return False
    if template.startswith(NEVER_PREFIXES) or ref_type in NEVER_REFS:
        return False
    if retry is not None:
        return retry
    return template not in CALLER_RETRIES and template != "guide_followup"


def payload(
    html: str | None, headers: dict[str, str] | None, attachments: list[Any] | None, reply_to: list[str] | None
) -> dict[str, Any] | None:
    """What a retry needs beyond the logged subject and body; None if it can't be kept."""
    files = []
    size = 0
    for name, content, mimetype in attachments or ():
        raw = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        size += len(raw)
        files.append([name, base64.b64encode(raw).decode("ascii"), mimetype])
    if size > MAX_PAYLOAD_BYTES:
        return None
    return {"html": html, "headers": dict(headers or {}), "reply_to": list(reply_to or []), "attachments": files}


def first_retry_at(now: datetime) -> datetime:
    return now + BACKOFF[0]


@dataclass
class RetryResult:
    stuck: int = 0  # rows found mid-send and marked failed
    retried: int = 0
    sent: int = 0
    failed: int = 0
    gave_up: int = 0

    def summary(self) -> str:
        return (
            f"{self.retried} retried ({self.sent} sent, {self.failed} failed), {self.gave_up} given up, "
            f"{self.stuck} found stuck"
        )


def _send(row: Notification) -> None:
    from .channels import get_channel

    data = row.retry_payload or {}
    extra: dict[str, Any] = {}
    if data.get("headers"):
        extra["headers"] = data["headers"]
    if data.get("reply_to"):
        extra["reply_to"] = data["reply_to"]
    if data.get("attachments"):
        extra["attachments"] = [(n, base64.b64decode(c), m) for n, c, m in data["attachments"]]
    get_channel(row.channel).send(row.recipient, row.subject, row.body, html=data.get("html"), **extra)


def mark_stuck(now: datetime) -> int:
    """Rows left "queued" past STUCK_AFTER: failed. Those with a payload are due for a retry straight away."""
    stuck = Notification.objects.filter(status=Notification.Status.QUEUED, created_at__lt=now - STUCK_AFTER)
    note = "Interrupted before it finished sending (the process stopped); marked failed by the jobs run."
    n = stuck.filter(retry_payload__isnull=False).update(status=Notification.Status.FAILED, error=note, next_retry_at=now)
    n += stuck.update(status=Notification.Status.FAILED, error=note, next_retry_at=None)
    return n


def _give_up(row: Notification, reason: str = "") -> None:
    fields: dict[str, Any] = {"next_retry_at": None, "retry_payload": None}
    if reason:
        fields["error"] = reason
    Notification.objects.filter(pk=row.pk).update(**fields)


def run(now: datetime | None = None, limit: int = 200) -> RetryResult:
    """Mark stuck rows failed, then send due retries. Safe to run concurrently (each retry is claimed first)."""
    now = now or timezone.now()
    result = RetryResult(stuck=mark_stuck(now))
    due = (
        Notification.objects.filter(status=Notification.Status.FAILED, next_retry_at__lte=now)
        .filter(retry_payload__isnull=False)
        .order_by("next_retry_at")[:limit]
    )
    failures_in_a_row = 0
    for row in due:
        if row.attempts >= MAX_ATTEMPTS or now - row.created_at > GIVE_UP_AFTER:
            _give_up(row)
            result.gave_up += 1
            continue
        attempt = row.attempts + 1
        wait = BACKOFF[attempt - 1] if attempt < MAX_ATTEMPTS else None
        # The claim: one runner wins, and a crash from here leaves the row due again after `wait`.
        claimed = Notification.objects.filter(
            pk=row.pk, status=Notification.Status.FAILED, attempts=row.attempts, next_retry_at=row.next_retry_at
        ).update(attempts=attempt, next_retry_at=now + wait if wait else now + STUCK_AFTER)
        if not claimed:
            continue
        result.retried += 1
        try:
            _send(row)
        except Exception as exc:  # noqa: BLE001 -- one bad row or a down server must not stop the jobs loop
            result.failed += 1
            failures_in_a_row += 1
            error = f"Attempt {attempt} of {MAX_ATTEMPTS} failed: {type(exc).__name__}: {exc}"[:2000]
            logger.warning("Retry of notification %s to %s failed: %s", row.pk, row.recipient, error)
            if wait is None:
                _give_up(row, error)
                result.gave_up += 1
            else:
                Notification.objects.filter(pk=row.pk).update(error=error)
            if failures_in_a_row >= STOP_AFTER_FAILURES:
                break
            continue
        failures_in_a_row = 0
        result.sent += 1
        Notification.objects.filter(pk=row.pk).update(
            status=Notification.Status.SENT, sent_at=timezone.now(), next_retry_at=None, retry_payload=None,
            error="",  # `attempts` keeps the count
        )
    return result
