"""
FR-22: scheduled reports. Each ReportSchedule covers the period that just ended and goes out around its hour:

- daily:   yesterday, every day;
- weekly:  last Monday to Sunday, on the chosen weekday;
- monthly: last calendar month, on the chosen day of the month (1-28, so every month has it);
- custom:  the last N days, every N days from the first send.

`send_due()` is a jobs-loop job (z_main run_jobs, hourly): it claims each due schedule by moving `next_run_at` on
(so two runs never send it twice), then emails it (report_mail.send_report). A schedule whose owner's plan no
longer includes reports_pro is kept but not sent ("Paused") until the plan has it again.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from django.utils import timezone

from z_auth import entitlements as plan
from z_auth.entitlements import entitlements_for

from .models import ReportSchedule
from .report_mail import send_report

logger = logging.getLogger("zinwe.reports")

MAX_SCHEDULES = 10
PAUSED_PLAN = "Paused: your plan doesn't include scheduled reports."


def _at(day: date, hour: int) -> datetime:
    return timezone.make_aware(datetime.combine(day, datetime.min.time()) + timedelta(hours=hour),
                               timezone.get_current_timezone())


def next_run(schedule: ReportSchedule, after: datetime) -> datetime:
    """The first send time strictly after `after` (aware)."""
    if schedule.frequency == ReportSchedule.Frequency.CUSTOM and schedule.next_run_at is not None:
        step = timedelta(days=max(2, schedule.every_days))
        due = schedule.next_run_at
        while due <= after:
            due += step
        return due
    day = timezone.localtime(after).date()
    for _ in range(400):
        candidate = _at(day, schedule.send_hour)
        if candidate > after and _matches(schedule, day):
            return candidate
        day += timedelta(days=1)
    raise ValueError("No send day found for this schedule.")  # unreachable with valid settings


def _matches(schedule: ReportSchedule, day: date) -> bool:
    if schedule.frequency == ReportSchedule.Frequency.WEEKLY:
        return day.weekday() == schedule.weekday
    if schedule.frequency == ReportSchedule.Frequency.MONTHLY:
        return day.day == schedule.month_day
    return True  # daily; custom's first send


def period_for(schedule: ReportSchedule, run_day: date) -> tuple[date, date]:
    """The period a send on `run_day` (local) covers: always days that have fully ended."""
    if schedule.frequency == ReportSchedule.Frequency.WEEKLY:
        end = run_day - timedelta(days=run_day.weekday() + 1)  # the last Sunday before the send day
        return end - timedelta(days=6), end
    if schedule.frequency == ReportSchedule.Frequency.MONTHLY:
        end = run_day.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    if schedule.frequency == ReportSchedule.Frequency.CUSTOM:
        return run_day - timedelta(days=max(2, schedule.every_days)), run_day - timedelta(days=1)
    yesterday = run_day - timedelta(days=1)
    return yesterday, yesterday


def describe(schedule: ReportSchedule) -> str:
    """ "Every Monday around 9 AM", for the dashboard."""
    hour = schedule.send_hour
    when = f"around {(hour % 12) or 12} {'AM' if hour < 12 else 'PM'}"
    if schedule.frequency == ReportSchedule.Frequency.WEEKLY:
        days = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
        return f"Every {days[schedule.weekday]} {when}, for the week before"
    if schedule.frequency == ReportSchedule.Frequency.MONTHLY:
        n = schedule.month_day
        suffix = "th" if 11 <= n <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        return f"On the {n}{suffix} of every month {when}, for the month before"
    if schedule.frequency == ReportSchedule.Frequency.CUSTOM:
        return f"Every {schedule.every_days} days {when}, for those {schedule.every_days} days"
    return f"Every day {when}, for the day before"


def schedule_first_run(schedule: ReportSchedule, now: datetime | None = None) -> None:
    """Set `next_run_at` for a new or changed schedule (not saved)."""
    schedule.next_run_at = None
    schedule.next_run_at = next_run(schedule, now or timezone.now())


@dataclass
class ScheduledReportsResult:
    sent: int = 0
    skipped: int = 0
    failed: int = 0

    def summary(self) -> str:
        return f"{self.sent} sent, {self.skipped} skipped, {self.failed} failed"


def send_due(now: datetime | None = None) -> ScheduledReportsResult:
    """Email every schedule whose time has come (jobs loop, hourly)."""
    now = now or timezone.now()
    result = ScheduledReportsResult()
    due = ReportSchedule.objects.filter(is_active=True, next_run_at__lte=now, owner__is_active=True).select_related("owner", "member")
    for schedule in due:
        planned = schedule.next_run_at
        following = next_run(schedule, now)
        # Claim it: only the run that moves next_run_at on sends it.
        if not ReportSchedule.objects.filter(pk=schedule.pk, next_run_at=planned).update(next_run_at=following):
            continue
        schedule.next_run_at = following
        if not entitlements_for(schedule.owner).has(plan.REPORTS_PRO):
            ReportSchedule.objects.filter(pk=schedule.pk).update(last_status=PAUSED_PLAN)
            result.skipped += 1
            continue
        if schedule.member_id and not schedule.member.is_active:
            ReportSchedule.objects.filter(pk=schedule.pk).update(
                last_status="Not sent: this person was removed from your team.")
            result.skipped += 1
            continue
        start, end = period_for(schedule, timezone.localtime(planned).date())
        try:
            delivery = send_report(
                schedule.owner, start, end, member=schedule.member, to_owner=schedule.send_to_owner,
                to_members=schedule.send_to_members, emails=schedule.extra_emails, include_csv=schedule.include_csv,
                schedule=schedule,
            )
        except Exception as exc:  # noqa: BLE001 -- one schedule must never stop the others
            logger.exception("Scheduled report %s failed", schedule.pk)
            ReportSchedule.objects.filter(pk=schedule.pk).update(last_status=f"Failed: {type(exc).__name__}"[:200])
            result.failed += 1
            continue
        ReportSchedule.objects.filter(pk=schedule.pk).update(last_sent_at=now, last_status=delivery.detail[:200])
        if delivery.status in ("sent", "partial"):
            result.sent += 1
        elif delivery.status == "failed":
            result.failed += 1
        else:
            result.skipped += 1
    return result
