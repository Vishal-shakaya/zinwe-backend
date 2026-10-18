"""
"How are we doing?" emails (run by `manage.py send_feedback_requests`, or the `run_jobs` loop).

The dashboard has no Feedback page in its menu any more: owners rate us from the account
menu's feedback modal, or the link in this email, which lands on `/app/feedback`. Each
rating in the email is a one-tap link that opens the page with that score picked.

Who gets one:
- an active, verified owner (not Zinwe staff) whose account is at least
  ZINWE_FEEDBACK_REQUEST_AFTER_DAYS old (default 14), so they've actually used it;
- who hasn't sent feedback, and hasn't been asked, in the last
  ZINWE_FEEDBACK_REQUEST_EVERY_DAYS (default 90).

"Asked" is read from the notification log (template `feedback_request`, status sent), so a
failed send is simply retried next run and no extra table is needed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.conf import settings
from django.db.models import Exists, OuterRef
from django.utils import timezone

from z_notification.models import Notification
from z_notification.services import notify

from .models import Feedback, User

logger = logging.getLogger("zinwe.feedback_requests")

TEMPLATE = "feedback_request"
RATINGS = range(1, 11)
# Keep one run's batch small; the rest go out next round.
BATCH = 200


@dataclass
class RunResult:
    sent: list[str] = field(default_factory=list)
    failed: int = 0

    def summary(self) -> str:
        return f"{len(self.sent)} feedback request(s), {self.failed} failed"


def _days(name: str, default: int) -> timedelta:
    return timedelta(days=int(getattr(settings, name, default)))


def due_owners(now: datetime) -> list[User]:
    after = _days("ZINWE_FEEDBACK_REQUEST_AFTER_DAYS", 14)
    every = _days("ZINWE_FEEDBACK_REQUEST_EVERY_DAYS", 90)
    since = now - every
    recent_feedback = Feedback.objects.filter(owner=OuterRef("pk"), created_at__gte=since)
    recent_request = Notification.objects.filter(
        template=TEMPLATE,
        recipient__iexact=OuterRef("email"),
        status=Notification.Status.SENT,
        created_at__gte=since,
    )
    return list(
        User.objects.filter(
            is_active=True,
            is_staff=False,
            email_verified_at__isnull=False,
            date_joined__lte=now - after,
        )
        .exclude(Exists(recent_feedback))
        .exclude(Exists(recent_request))
        .order_by("date_joined")[:BATCH]
    )


def _context(owner: User) -> dict:
    base = f"{settings.ZINWE_APP_URL}/app/feedback"
    return {
        "first_name": owner.get_short_name() if owner.full_name else "there",
        "feedback_url": base,
        "rating_links": [(n, f"{base}?rating={n}") for n in RATINGS],
    }


def send_feedback_requests(now: datetime | None = None, dry_run: bool = False) -> RunResult:
    now = now or timezone.now()
    result = RunResult()
    for owner in due_owners(now):
        if dry_run:
            result.sent.append(owner.email)
            continue
        sent = notify("email", owner.email, TEMPLATE, _context(owner), ref=owner)
        if sent is not None and sent.status == Notification.Status.SENT:
            result.sent.append(owner.email)
        else:
            result.failed += 1
            logger.warning("Feedback request to %s failed; will retry", owner.email)
    return result
