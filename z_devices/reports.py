"""
FR-17: a report for any period (a day, a week, a month or custom dates): per person, the days they worked, time
worked and on breaks, breaks and log-outs (and how many still wait for the owner's OK), and their alerts; for the
team, the top apps and websites. Shown on the dashboard's Reports page and downloaded as CSV.

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
from django.utils import timezone

from z_auth import entitlements as plan
from z_auth.entitlements import entitlements_for
from z_auth.models import User

from .models import DeviceEvent, Member, UsageMetric, WorkLog

MAX_DAYS = 366
TOP = 10


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


def _work_by_person(owner: User, start: date, end: date, now: datetime) -> dict[Any, dict[str, Any]]:
    lo, hi = _bounds(start, end)
    logs = (
        WorkLog.objects.filter(owner=owner, at__gte=lo, at__lt=hi)
        .select_related("member", "device")
        .order_by("at", "id")
    )
    days: dict[tuple[Any, date], list[WorkLog]] = defaultdict(list)
    names: dict[Any, tuple[str, int | None]] = {}
    for log in logs:
        key = ("m", log.member_id) if log.member_id else ("d", log.device_id)
        days[(key, timezone.localtime(log.at).date())].append(log)
        names[key] = (log.member.name if log.member else log.device.hostname, log.member_id)

    today = timezone.localdate()
    people: dict[Any, dict[str, Any]] = {}
    for (key, day), entries in days.items():
        row = people.setdefault(key, {
            "member_id": names[key][1], "name": names[key][0], "days_worked": 0, "worked_seconds": 0,
            "break_seconds": 0, "breaks": 0, "logouts": 0, "to_review": 0, "alerts": 0,
        })
        row["days_worked"] += 1
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
                row["worked_seconds"] += span
            elif entry.state == WorkLog.State.ON_BREAK:
                row["break_seconds"] += span
                row["breaks"] += 1
            else:
                row["logouts"] += 1
            if entry.approval == WorkLog.Approval.PENDING:
                row["to_review"] += 1
    return people


def build_report(owner: User, start: date, end: date, now: datetime | None = None) -> dict[str, Any]:
    """The report for `start`..`end` (local dates, both included; already clamped by `clamp_period`)."""
    now = now or timezone.now()
    ent = entitlements_for(owner)
    lo, hi = _bounds(start, end)
    people = _work_by_person(owner, start, end, now) if ent.has(plan.WORK_TIME) else {}

    from .services import _locked_kinds, confirmation_q

    alerts = (
        DeviceEvent.objects.filter(device__owner=owner, occurred_at__gte=lo, occurred_at__lt=hi)
        .exclude(_locked_kinds("type", ent))
        .exclude(confirmation_q())
    )
    for row in alerts.values("device__member_id", "device__member__name", "device_id", "device__hostname").annotate(n=Count("id")):
        key = ("m", row["device__member_id"]) if row["device__member_id"] else ("d", row["device_id"])
        person = people.setdefault(key, {
            "member_id": row["device__member_id"], "name": row["device__member__name"] or row["device__hostname"],
            "days_worked": 0, "worked_seconds": 0, "break_seconds": 0, "breaks": 0, "logouts": 0, "to_review": 0,
            "alerts": 0,
        })
        person["alerts"] += row["n"]

    def top(category: str) -> list[dict[str, Any]]:
        if not ent.has(plan.APP_USAGE if category == "app.foreground" else plan.WEB):
            return []
        return list(
            UsageMetric.objects.filter(device__owner=owner, category=category, bucket_start__gte=lo, bucket_start__lt=hi)
            .exclude(key="")
            .values("key")
            .annotate(seconds=Sum("seconds"), count=Sum("count"))
            .order_by("-seconds", "-count", "key")[:TOP]
        )

    rows = sorted(people.values(), key=lambda r: (-r["worked_seconds"], r["name"].lower()))
    totals = {k: sum(r[k] for r in rows) for k in ("worked_seconds", "break_seconds", "breaks", "logouts", "to_review", "alerts")}
    active = {m.pk: m.name for m in Member.objects.filter(owner=owner, is_active=True)}
    seen = {r["member_id"] for r in rows if r["member_id"]}
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "days": (end - start).days + 1,
        "people": rows,
        "no_activity": sorted(name for pk, name in active.items() if pk not in seen),
        "totals": {**totals, "alerts_all": alerts.count()},
        "top_apps": top("app.foreground"),
        "top_sites": top("web.domain"),
    }


def _hm(seconds: int) -> str:
    """3725 -> "1:02" (hours:minutes), what spreadsheets read as a time."""
    minutes = int(seconds) // 60
    return f"{minutes // 60}:{minutes % 60:02d}"


def report_csv(report: dict[str, Any], company: str) -> str:
    """The report as CSV: the per-person table, then the top apps and websites."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([f"Zinwe report for {company}", f"{report['from']} to {report['to']}"])
    w.writerow([])
    w.writerow(["Person", "Days worked", "Time worked (h:mm)", "On breaks (h:mm)", "Breaks", "Log-outs",
                "Waiting for your OK", "Alerts"])
    for r in report["people"]:
        w.writerow([r["name"], r["days_worked"], _hm(r["worked_seconds"]), _hm(r["break_seconds"]), r["breaks"],
                    r["logouts"], r["to_review"], r["alerts"]])
    t = report["totals"]
    w.writerow(["Everyone", "", _hm(t["worked_seconds"]), _hm(t["break_seconds"]), t["breaks"], t["logouts"],
                t["to_review"], t["alerts"]])
    if report["no_activity"]:
        w.writerow([])
        w.writerow(["No activity in this period", ", ".join(report["no_activity"])])
    for title, items in (("Top apps", report["top_apps"]), ("Top websites", report["top_sites"])):
        if items:
            w.writerow([])
            w.writerow([title, "Time (h:mm)"])
            for i in items:
                w.writerow([i["key"], _hm(i["seconds"] or 0)])
    return out.getvalue()
