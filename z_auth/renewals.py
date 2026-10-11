"""
Automatic renewal reminders for paid plans (run by `manage.py send_renewal_reminders`, or the
`run_jobs` loop the local start script keeps running).

For every active paid plan (not free, not a trial):
- ZINWE_RENEWAL_REMINDER_DAYS (7, 1) before it ends: one "your plan ends in N days" email per
  threshold. Only the nearest due one goes out (a plan first seen 1 day out gets the 1-day email,
  not both). Skipped while a renewal payment is waiting for approval.
- When it ends: it's marked expired and the owner gets one "your plan has expired" email
  (unless they've already moved to a newer subscription).

Each email is recorded as a RenewalReminder keyed on (subscription, kind, period_end), so a
reminder never repeats for the same period, and a renewal (which moves `ends_at`) starts fresh.
The row is claimed before sending and released if the send fails, so the next run retries.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from z_notification.models import Notification
from z_notification.services import notify

from .billing import days_left, expire_if_due, has_payment_under_review
from .models import RenewalReminder, Subscription

logger = logging.getLogger("zinwe.renewals")

Status = Subscription.Status
Kind = RenewalReminder.Kind

REMINDER_TEMPLATE = "renewal_reminder"
EXPIRED_TEMPLATE = "plan_expired"
# Expired emails only for plans that ended recently (never a flood of old history on first run).
EXPIRED_LOOKBACK = timedelta(days=3)


@dataclass
class RunResult:
    reminders: list[str] = field(default_factory=list)  # "owner@x: 7d"
    expired: list[str] = field(default_factory=list)
    skipped_under_review: int = 0
    failed: int = 0

    def summary(self) -> str:
        return (
            f"{len(self.reminders)} reminder(s), {len(self.expired)} expiry notice(s), "
            f"{self.skipped_under_review} skipped (renewal under review), {self.failed} failed"
        )


def _thresholds() -> list[int]:
    return sorted(set(getattr(settings, "ZINWE_RENEWAL_REMINDER_DAYS", (7, 1))))


def _due_kind(left: int) -> str | None:
    """The nearest threshold this many days-left falls under (7 → "7d", 1 → "1d")."""
    for days in _thresholds():
        if left <= days:
            return {7: Kind.DAYS_7, 1: Kind.DAYS_1}.get(days, f"{days}d")
    return None


def _paid(sub: Subscription) -> bool:
    return not sub.is_trial and sub.amount_inr > 0 and sub.ends_at is not None


def _context(sub: Subscription, now: datetime) -> dict:
    owner = sub.owner
    return {
        "first_name": owner.get_short_name() if owner.full_name else "there",
        "plan": sub.plan,
        "subscription": sub,
        "days_left": days_left(sub, now),
        "company": getattr(owner, "owned_company", None),
        "billing_url": f"{settings.ZINWE_APP_URL}/app/settings/billing",
    }


def _send_once(sub: Subscription, kind: str, template: str, now: datetime, dry_run: bool) -> bool | None:
    """Claim (sub, kind, period) and send. True = sent, None = already sent before, False = failed."""
    if RenewalReminder.objects.filter(subscription=sub, kind=kind, period_end=sub.ends_at).exists():
        return None
    if dry_run:
        return True
    try:
        with transaction.atomic():
            claim = RenewalReminder.objects.create(subscription=sub, kind=kind, period_end=sub.ends_at, sent_at=now)
    except IntegrityError:
        return None  # another run got it first
    sent = notify("email", sub.owner.email, template, _context(sub, now), ref=sub)
    if sent is None or sent.status != Notification.Status.SENT:
        claim.delete()  # let the next run try again
        logger.warning("Renewal %s email to %s failed; will retry", kind, sub.owner.email)
        return False
    return True


def send_due_reminders(now: datetime | None = None, dry_run: bool = False) -> RunResult:
    now = now or timezone.now()
    result = RunResult()
    horizon = now + timedelta(days=max(_thresholds()) + 1)

    active = (
        Subscription.objects.filter(status=Status.ACTIVE, is_trial=False, amount_inr__gt=0, ends_at__lte=horizon)
        .select_related("owner", "plan")
        .order_by("ends_at")
    )
    for sub in active:
        if sub.ends_at <= now:
            if not dry_run:
                expire_if_due(sub)  # handled with the expired ones below
            continue
        if has_payment_under_review(sub):
            result.skipped_under_review += 1
            continue
        kind = _due_kind(days_left(sub, now) or 0)
        if kind is None:
            continue
        outcome = _send_once(sub, kind, REMINDER_TEMPLATE, now, dry_run)
        if outcome:
            result.reminders.append(f"{sub.owner.email}: {kind}")
        elif outcome is False:
            result.failed += 1

    ended = (
        Subscription.objects.filter(
            status__in=[Status.EXPIRED, Status.ACTIVE] if dry_run else [Status.EXPIRED],
            is_trial=False, amount_inr__gt=0,
            ends_at__lte=now, ends_at__gte=now - EXPIRED_LOOKBACK,
        )
        .select_related("owner", "plan")
    )
    for sub in ended:
        if not _paid(sub):
            continue
        # Their newest subscription, read without side effects (current_subscription() would save expiries).
        latest = Subscription.objects.filter(owner=sub.owner).order_by("-created_at", "-id").first()
        if latest is None or latest.pk != sub.pk:
            continue  # they've already chosen a new plan
        outcome = _send_once(sub, Kind.EXPIRED, EXPIRED_TEMPLATE, now, dry_run)
        if outcome:
            result.expired.append(sub.owner.email)
        elif outcome is False:
            result.failed += 1
    return result
