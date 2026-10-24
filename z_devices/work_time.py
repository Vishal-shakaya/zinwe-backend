"""
FIX-011: time worked and on breaks per local calendar day, shared by Activity (GET work-log/) and reports.

A person's work log is a list of changes (log in, break, back, log out). Each change opens a stretch that runs until
their next change. A stretch that crosses midnight is split between the days: log in 22:00, log out 02:00 = 2 h on
each day. Breaks are split the same way (the break itself is counted once, on the day it began).

- A stretch inside one day counts in full. The last, still-open stretch counts up to "now" when it began today; on a
  past day an open stretch (they never logged out) isn't counted and the day is flagged `open_ended`.
- A stretch that crosses midnight counts, split by day, when it lasts at most MAX_OVERNIGHT. Longer is a forgotten
  log-out: not counted, and its first day is flagged `open_ended`. One still running (no next change yet) counts
  across midnight only while the PC is online and still reports that state.
- A day worked is a day the person logged in (started a shift). A shift over midnight is one day worked, on the day
  it began; a day with only a log-out (or only the end of last night's shift) isn't one.

"A day" is a calendar day in one time zone, chosen in one place: work_zone(). Today that is the server's
(settings.TIME_ZONE, Asia/Kolkata); a company time zone (GAP-14) plugs in there.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, Iterable

from django.conf import settings
from django.utils import timezone

from .models import Device, WorkLog

# The longest stretch (working or on a break, with no change in between) that may cross midnight.
MAX_OVERNIGHT = timedelta(hours=16)

Key = tuple[str, int]


def work_zone(owner: Any = None) -> tzinfo:
    """The time zone a work day is counted in. The one place to change it (GAP-14: the company's own zone)."""
    return timezone.get_current_timezone()


def day_start(day: date, tz: tzinfo) -> datetime:
    return timezone.make_aware(datetime.combine(day, datetime.min.time()), tz)


def day_bounds(start: date, end: date, tz: tzinfo) -> tuple[datetime, datetime]:
    """[local midnight of `start`, local midnight after `end`)."""
    return day_start(start, tz), day_start(end + timedelta(days=1), tz)


def local_day(at: datetime, tz: tzinfo) -> date:
    return at.astimezone(tz).date()


def person_key(log: WorkLog) -> Key:
    """A member, or the PC when it has no member (as Activity and reports group people)."""
    return ("m", log.member_id) if log.member_id else ("d", log.device_id)


@dataclass
class DayWork:
    """One person's work on one day."""

    worked: int = 0
    on_break: int = 0
    breaks: int = 0  # breaks that began this day
    logouts: int = 0
    to_review: int = 0  # breaks and log-outs this day still waiting for the owner's OK
    logged_in: bool = False  # started a shift this day (the "day worked" rule)
    first_in: datetime | None = None  # first log-in this day
    last_out: datetime | None = None  # last log-out this day
    open_ended: bool = False  # a stretch began this day that couldn't be counted (no log-out)
    carried_in: WorkLog | None = None  # the change whose stretch was running at this day's midnight
    continues: bool = False  # the day's last stretch runs on past midnight into the next day
    has_entries: bool = False

    @property
    def active(self) -> bool:
        return self.has_entries or self.worked > 0 or self.on_break > 0


def _online(device: Device, now: datetime) -> bool:
    if device.last_seen_at is None or device.is_revoked:
        return False
    return now - device.last_seen_at <= timedelta(seconds=settings.ZINWE_DEVICE_OFFLINE_AFTER_SECONDS)


def _until(entry: WorkLog, nxt: WorkLog | None, now: datetime, tz: tzinfo) -> datetime | None:
    """When `entry`'s stretch ends for counting, or None when it can't be counted."""
    if nxt is None:
        if local_day(entry.at, tz) == local_day(now, tz):
            return now  # today's open stretch runs to now
        device = entry.device
        running = _online(device, now) and device.work_state == entry.state
        return now if running and now - entry.at <= MAX_OVERNIGHT else None
    if local_day(entry.at, tz) == local_day(nxt.at, tz):
        return nxt.at
    return nxt.at if nxt.at - entry.at <= MAX_OVERNIGHT else None


def split_by_day(seq: list[WorkLog], lo: datetime, hi: datetime, now: datetime, tz: tzinfo) -> dict[date, DayWork]:
    """
    One person's days in [lo, hi). `seq` is their changes oldest first: every change in [lo, hi), plus their last
    change before `lo` and first change at/after `hi` when those are within MAX_OVERNIGHT (load_window() fetches
    exactly that), so stretches crossing the edges are split correctly.
    """
    days: dict[date, DayWork] = defaultdict(DayWork)
    prev_counted = False
    for i, entry in enumerate(seq):
        prev = seq[i - 1] if i else None
        nxt = seq[i + 1] if i + 1 < len(seq) else None
        day = local_day(entry.at, tz)
        inside = lo <= entry.at < hi
        if inside:
            w = days[day]
            w.has_entries = True
            # A new shift: nothing before it, a log-out before it, or the stretch before it wasn't counted.
            if entry.state != WorkLog.State.ENDED and (prev is None or prev.state == WorkLog.State.ENDED or not prev_counted):
                w.logged_in = True
                w.first_in = w.first_in or entry.at
            if entry.state == WorkLog.State.ON_BREAK:
                w.breaks += 1
            elif entry.state == WorkLog.State.ENDED:
                w.logouts += 1
                w.last_out = entry.at
            if entry.approval == WorkLog.Approval.PENDING:
                w.to_review += 1
        prev_counted = False
        if entry.state == WorkLog.State.ENDED:
            continue
        until = _until(entry, nxt, now, tz)
        if until is None:
            if inside:
                days[day].open_ended = True
            continue
        prev_counted = True
        field = "worked" if entry.state == WorkLog.State.WORKING else "on_break"
        _spread(days, entry, until, lo, hi, field, tz)
    return dict(days)


def _spread(days: dict[date, DayWork], entry: WorkLog, until: datetime, lo: datetime, hi: datetime, field: str,
            tz: tzinfo) -> None:
    """Add the stretch [entry.at, until), clipped to [lo, hi), to each local day it covers."""
    a, b = max(entry.at, lo), min(until, hi)
    while a < b:
        day = local_day(a, tz)
        midnight = day_start(day + timedelta(days=1), tz)
        upto = min(b, midnight)
        w = days[day]
        setattr(w, field, getattr(w, field) + int((upto - a).total_seconds()))
        if entry.at < day_start(day, tz):
            w.carried_in = entry
        if until > midnight:
            w.continues = True
        a = upto


def load_window(logs: Iterable[WorkLog], lo: datetime, hi: datetime) -> dict[Key, list[WorkLog]]:
    """Group changes from [lo - MAX_OVERNIGHT, hi + MAX_OVERNIGHT) per person, keeping only the edge changes that
    matter: the last before `lo` and the first at/after `hi`."""
    grouped: dict[Key, list[WorkLog]] = defaultdict(list)
    for log in logs:
        grouped[person_key(log)].append(log)
    out: dict[Key, list[WorkLog]] = {}
    for key, entries in grouped.items():
        entries.sort(key=lambda e: (e.at, e.id))
        before = [e for e in entries if e.at < lo][-1:]
        inside = [e for e in entries if lo <= e.at < hi]
        after = [e for e in entries if e.at >= hi][:1]
        out[key] = before + inside + after
    return out


def window_queryset(qs: Any, lo: datetime, hi: datetime, since: datetime | None = None) -> Any:
    """The work-log rows load_window() needs (never before the plan's history floor `since`)."""
    floor = lo - MAX_OVERNIGHT if since is None else max(lo - MAX_OVERNIGHT, since)
    return (qs.filter(at__gte=floor, at__lt=hi + MAX_OVERNIGHT)
            .select_related("member", "device").order_by("at", "id"))


def work_days(qs: Any, lo: datetime, hi: datetime, now: datetime, tz: tzinfo,
              since: datetime | None = None) -> list[tuple[Key, WorkLog, dict[date, DayWork]]]:
    """Each person's days in [lo, hi): (key, one of their changes, for the name), {day: DayWork}). People with nothing
    in the period (only an uncounted stretch from before it) are left out."""
    out = []
    for key, seq in load_window(window_queryset(qs, lo, hi, since), lo, hi).items():
        days = {d: w for d, w in split_by_day(seq, lo, hi, now, tz).items() if w.active}
        if days:
            sample = next((e for e in seq if lo <= e.at < hi), seq[0])
            out.append((key, sample, days))
    return out
