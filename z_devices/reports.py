"""
FR-17 / FR-22: a report for any period (a day, a week, a month or custom dates), for the whole team or one person.

Per person: the days they worked, time worked and on breaks, breaks and log-outs (and how many still wait for the
owner's OK), their usual start and finish time, time active on the PC (app time), internet data used, and alerts.
For the period: a day-by-day trend and the top apps, websites and data-hungry sites. One person's report also
lists each of their days. Shown on the dashboard's Reports page, downloaded as CSV or PDF (report_pdf.py) and
emailed (report_mail.py, report_schedules.py).

Worked time follows the Activity page's rule (features/workDay.ts): each change in the work log runs until the
next one on that day; the last stretch runs to "now" today, and on a past day an unfinished stretch (they never
logged out) isn't counted.
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from z_auth import entitlements as plan
from z_auth.entitlements import entitlements_for
from z_auth.models import User

from .models import DeviceEvent, Member, UsageMetric, WorkLog

MAX_DAYS = 366
TOP = 10

ACTIVE = "app.foreground"  # seconds an app was in front while the PC was in use (agent 1.1.1+)
DATA = "web.data.app"  # bytes each app sent and received
DATA_SITE = "web.data.site"


class BadPeriod(ValueError):
    """The dates are reversed, in the future, too long, or before the plan's history."""


def clamp_period(owner: User, start: date, end: date) -> tuple[date, date]:
    """The period the owner may see: never in the future, never before the plan keeps history."""
    today = timezone.localdate()
    if start > end:
        raise BadPeriod("The start date is after the end date.")
    if start > today:
        raise BadPeriod("That period hasn't started yet.")
    end = min(end, today)
    if (end - start).days + 1 > MAX_DAYS:
        raise BadPeriod("Pick a period of a year or less.")
    floor = entitlements_for(owner).history_since()
    if floor is not None:
        start = max(start, timezone.localtime(floor).date())
    return start, end


def _bounds(start: date, end: date) -> tuple[datetime, datetime]:
    tz = timezone.get_current_timezone()
    lo = timezone.make_aware(datetime.combine(start, datetime.min.time()), tz)
    hi = timezone.make_aware(datetime.combine(end + timedelta(days=1), datetime.min.time()), tz)
    return lo, hi


def _minutes(at: datetime) -> int:
    """Minutes since local midnight: 9:30 AM -> 570."""
    local = timezone.localtime(at)
    return local.hour * 60 + local.minute


def _person(member_id: int | None, name: str) -> dict[str, Any]:
    return {
        "member_id": member_id, "name": name, "days_worked": 0, "worked_seconds": 0, "break_seconds": 0,
        "breaks": 0, "logouts": 0, "to_review": 0, "alerts": 0, "active_seconds": 0, "data_bytes": 0,
        "first_in": None, "last_out": None,
        # running sums for the averages, dropped before returning
        "_in": [], "_out": [],
    }


def _day(day: date) -> dict[str, Any]:
    return {"date": day.isoformat(), "worked_seconds": 0, "break_seconds": 0, "breaks": 0, "alerts": 0,
            "active_seconds": 0, "data_bytes": 0, "people": 0, "first_in": None, "last_out": None}


class _Collector:
    """People (keyed by member, or by PC when it has no member) and days, filled from each data source."""

    def __init__(self, start: date, end: date) -> None:
        self.people: dict[Any, dict[str, Any]] = {}
        self.daily = {start + timedelta(days=i): _day(start + timedelta(days=i)) for i in range((end - start).days + 1)}
        # One person's own days (only used for a person's report).
        self.person_days: dict[date, dict[str, Any]] = {}

    def person(self, key: Any, member_id: int | None, name: str) -> dict[str, Any]:
        return self.people.setdefault(key, _person(member_id, name))

    def person_day(self, day: date) -> dict[str, Any]:
        return self.person_days.setdefault(day, _day(day))


def _work(c: _Collector, owner: User, lo: datetime, hi: datetime, now: datetime, member: Member | None) -> None:
    logs = WorkLog.objects.filter(owner=owner, at__gte=lo, at__lt=hi)
    if member is not None:
        logs = logs.filter(member=member)
    days: dict[tuple[Any, date], list[WorkLog]] = defaultdict(list)
    names: dict[Any, tuple[str, int | None]] = {}
    for log in logs.select_related("member", "device").order_by("at", "id"):
        key = ("m", log.member_id) if log.member_id else ("d", log.device_id)
        days[(key, timezone.localtime(log.at).date())].append(log)
        names[key] = (log.member.name if log.member else log.device.hostname, log.member_id)

    today = timezone.localdate()
    for (key, day), entries in days.items():
        row = c.person(key, names[key][1], names[key][0])
        trend = c.daily.get(day)
        mine = c.person_day(day) if member is not None else None
        row["days_worked"] += 1
        worked = on_break = breaks = 0
        for i, entry in enumerate(entries):
            nxt = entries[i + 1] if i + 1 < len(entries) else None
            if nxt is not None:
                until: datetime | None = nxt.at
            elif day == today and entry.state != WorkLog.State.ENDED:
                until = now
            else:
                until = None
            span = max(0, int((until - entry.at).total_seconds())) if until is not None else 0
            if entry.state == WorkLog.State.WORKING:
                worked += span
            elif entry.state == WorkLog.State.ON_BREAK:
                on_break += span
                breaks += 1
            else:
                row["logouts"] += 1
            if entry.approval == WorkLog.Approval.PENDING:
                row["to_review"] += 1
        first_in = next((_minutes(e.at) for e in entries if e.state == WorkLog.State.WORKING), None)
        last_out = next((_minutes(e.at) for e in reversed(entries) if e.state == WorkLog.State.ENDED), None)
        if first_in is not None:
            row["_in"].append(first_in)
        if last_out is not None:
            row["_out"].append(last_out)
        row["worked_seconds"] += worked
        row["break_seconds"] += on_break
        row["breaks"] += breaks
        if trend is not None:
            trend["worked_seconds"] += worked
            trend["break_seconds"] += on_break
            trend["breaks"] += breaks
            trend["people"] += 1
        if mine is not None:
            mine.update(worked_seconds=worked, break_seconds=on_break, breaks=breaks, first_in=first_in,
                        last_out=last_out, people=1)


def _alerts(c: _Collector, owner: User, lo: datetime, hi: datetime, ent: Any, member: Member | None):  # type: ignore[no-untyped-def]
    from .services import _locked_kinds, confirmation_q

    alerts = (
        DeviceEvent.objects.filter(device__owner=owner, occurred_at__gte=lo, occurred_at__lt=hi)
        .exclude(_locked_kinds("type", ent))
        .exclude(confirmation_q())
    )
    if member is not None:
        alerts = alerts.filter(device__member=member)
    tz = timezone.get_current_timezone()
    rows = (
        alerts.annotate(day=TruncDate("occurred_at", tzinfo=tz))
        .values("device__member_id", "device__member__name", "device_id", "device__hostname", "day")
        .annotate(n=Count("id"))
    )
    for row in rows:
        key = ("m", row["device__member_id"]) if row["device__member_id"] else ("d", row["device_id"])
        c.person(key, row["device__member_id"], row["device__member__name"] or row["device__hostname"])["alerts"] += row["n"]
        if row["day"] in c.daily:
            c.daily[row["day"]]["alerts"] += row["n"]
        if member is not None:
            c.person_day(row["day"])["alerts"] += row["n"]
    return alerts


def _usage(c: _Collector, owner: User, lo: datetime, hi: datetime, ent: Any, member: Member | None) -> None:
    """Active time on the PC (app time) and internet data, per person and per day."""
    wanted = {cat: field for cat, field, module in ((ACTIVE, "seconds", plan.APP_USAGE), (DATA, "bytes", plan.WEB))
              if ent.has(module)}
    if not wanted:
        return
    metrics = UsageMetric.objects.filter(device__owner=owner, category__in=list(wanted), bucket_start__gte=lo,
                                         bucket_start__lt=hi)
    if member is not None:
        metrics = metrics.filter(device__member=member)
    tz = timezone.get_current_timezone()
    rows = (
        metrics.annotate(day=TruncDate("bucket_start", tzinfo=tz))
        .values("device__member_id", "device__member__name", "device_id", "device__hostname", "day", "category")
        .annotate(seconds=Sum("seconds"), bytes=Sum("bytes"))
    )
    for row in rows:
        field, value = ("active_seconds", row["seconds"] or 0) if row["category"] == ACTIVE else ("data_bytes", row["bytes"] or 0)
        if not value:
            continue
        key = ("m", row["device__member_id"]) if row["device__member_id"] else ("d", row["device_id"])
        c.person(key, row["device__member_id"], row["device__member__name"] or row["device__hostname"])[field] += value
        if row["day"] in c.daily:
            c.daily[row["day"]][field] += value
        if member is not None:
            c.person_day(row["day"])[field] += value


def _top(owner: User, lo: datetime, hi: datetime, ent: Any, member: Member | None, category: str, by: str) -> list[dict[str, Any]]:
    module = {ACTIVE: plan.APP_USAGE, "web.domain": plan.WEB, DATA_SITE: plan.WEB}[category]
    if not ent.has(module):
        return []
    metrics = UsageMetric.objects.filter(device__owner=owner, category=category, bucket_start__gte=lo, bucket_start__lt=hi)
    if member is not None:
        metrics = metrics.filter(device__member=member)
    order = ("-bytes", "key") if by == "bytes" else ("-seconds", "-count", "key")
    rows = metrics.exclude(key="").values("key").annotate(seconds=Sum("seconds"), count=Sum("count"), bytes=Sum("bytes"))
    out = []
    for row in rows.order_by(*order)[:TOP]:
        if by == "bytes" and not row["bytes"]:
            continue
        item = {"key": row["key"], "seconds": row["seconds"] or 0, "count": row["count"] or 0}
        if by == "bytes":
            item["bytes"] = row["bytes"] or 0
        out.append(item)
    return out


def _average(values: list[int]) -> int | None:
    return round(sum(values) / len(values)) if values else None


def build_report(
    owner: User, start: date, end: date, now: datetime | None = None, member: Member | None = None,
) -> dict[str, Any]:
    """The report for `start`..`end` (local dates, both included; already clamped by `clamp_period`), for the whole
    team or, with `member`, for that one person."""
    now = now or timezone.now()
    ent = entitlements_for(owner)
    lo, hi = _bounds(start, end)
    c = _Collector(start, end)
    if ent.has(plan.WORK_TIME):
        _work(c, owner, lo, hi, now, member)
    alerts = _alerts(c, owner, lo, hi, ent, member)
    _usage(c, owner, lo, hi, ent, member)

    rows = []
    for row in c.people.values():
        row["first_in"], row["last_out"] = _average(row.pop("_in")), _average(row.pop("_out"))
        rows.append(row)
    rows.sort(key=lambda r: (-r["worked_seconds"], -r["active_seconds"], r["name"].lower()))
    keys = ("worked_seconds", "break_seconds", "breaks", "logouts", "to_review", "alerts", "active_seconds", "data_bytes")
    totals = {k: sum(r[k] for r in rows) for k in keys}
    totals["alerts_all"] = alerts.count()
    totals["people_worked"] = sum(1 for r in rows if r["days_worked"] > 0)
    totals["person_days"] = sum(r["days_worked"] for r in rows)

    report: dict[str, Any] = {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "days": (end - start).days + 1,
        "member": None,
        "people": rows,
        "no_activity": [],
        "totals": totals,
        "daily": list(c.daily.values()),
        "top_apps": _top(owner, lo, hi, ent, member, ACTIVE, "seconds"),
        "top_sites": _top(owner, lo, hi, ent, member, "web.domain", "seconds"),
        "top_data": _top(owner, lo, hi, ent, member, DATA_SITE, "bytes"),
    }
    if member is None:
        seen = {r["member_id"] for r in rows if r["member_id"]}
        report["no_activity"] = sorted(m.name for m in Member.objects.filter(owner=owner, is_active=True) if m.pk not in seen)
    else:
        report["member"] = {"id": member.pk, "name": member.name, "email": member.email, "role": member.role}
        report["person_days"] = [c.person_days[d] for d in sorted(c.person_days)]
    return report


def report_title(report: dict[str, Any]) -> str:
    """ "Team report" or "Asha Rao's report"."""
    member = report.get("member")
    return f"{member['name']}'s report" if member else "Team report"


def period_label(report: dict[str, Any]) -> str:
    """ "21 Oct 2026" or "14 Oct to 20 Oct 2026"."""
    start, end = date.fromisoformat(report["from"]), date.fromisoformat(report["to"])
    if start == end:
        return f"{end.day} {end:%b %Y}"
    first = f"{start.day} {start:%b}" if start.year == end.year else f"{start.day} {start:%b %Y}"
    return f"{first} to {end.day} {end:%b %Y}"


def hm(seconds: int) -> str:
    """3725 -> "1:02" (hours:minutes), what spreadsheets read as a time."""
    minutes = int(seconds) // 60
    return f"{minutes // 60}:{minutes % 60:02d}"


def clock(minutes: int | None) -> str:
    """570 -> "9:30 AM"; None -> "-"."""
    if minutes is None:
        return "-"
    h, m = divmod(int(minutes), 60)
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def size(num: int) -> str:
    """Bytes as "1.2 GB"."""
    value = float(max(0, num or 0))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def report_csv(report: dict[str, Any], company: str) -> str:
    """The report as CSV: the per-person table, a person's days, then the top apps, websites and data use."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([f"Zinwe {report_title(report).lower()} for {company}", f"{report['from']} to {report['to']}"])
    w.writerow([])
    w.writerow(["Person", "Days worked", "Time worked (h:mm)", "On breaks (h:mm)", "Breaks", "Log-outs",
                "Waiting for your OK", "Alerts", "Usual start", "Usual finish", "Active on PC (h:mm)",
                "Internet data (MB)"])
    for r in report["people"]:
        w.writerow([r["name"], r["days_worked"], hm(r["worked_seconds"]), hm(r["break_seconds"]), r["breaks"],
                    r["logouts"], r["to_review"], r["alerts"], clock(r["first_in"]), clock(r["last_out"]),
                    hm(r["active_seconds"]), round(r["data_bytes"] / 1_048_576, 1)])
    t = report["totals"]
    w.writerow(["Everyone", t["person_days"], hm(t["worked_seconds"]), hm(t["break_seconds"]), t["breaks"],
                t["logouts"], t["to_review"], t["alerts"], "", "", hm(t["active_seconds"]),
                round(t["data_bytes"] / 1_048_576, 1)])
    if report["no_activity"]:
        w.writerow([])
        w.writerow(["No activity in this period", ", ".join(report["no_activity"])])
    if report.get("person_days"):
        w.writerow([])
        w.writerow(["Day", "Started", "Finished", "Worked (h:mm)", "On breaks (h:mm)", "Breaks", "Alerts",
                    "Active on PC (h:mm)"])
        for d in report["person_days"]:
            w.writerow([d["date"], clock(d["first_in"]), clock(d["last_out"]), hm(d["worked_seconds"]),
                        hm(d["break_seconds"]), d["breaks"], d["alerts"], hm(d["active_seconds"])])
    for title, items in (("Top apps", report["top_apps"]), ("Top websites", report["top_sites"])):
        if items:
            w.writerow([])
            w.writerow([title, "Time (h:mm)"])
            for i in items:
                w.writerow([i["key"], hm(i["seconds"] or 0)])
    if report["top_data"]:
        w.writerow([])
        w.writerow(["Most internet data", "MB"])
        for i in report["top_data"]:
            w.writerow([i["key"], round(i["bytes"] / 1_048_576, 1)])
    return out.getvalue()
