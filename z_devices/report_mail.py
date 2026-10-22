"""
FR-22: email a report, now (Reports page "Email report") or on a schedule (report_schedules.py).

Who gets what (privacy first, nobody sees a colleague's numbers unless the owner sends them the team report):
- the owner and up to five other addresses the owner typed: the report as shown (team or one person), as a PDF
  (and CSV when asked) with a short summary in the email;
- with "each person gets their own report": every person in it receives only their own report (for a person's
  report, just that person). People with nothing recorded in the period are skipped.
Every send is recorded as a ReportDelivery (the Reports page's "Sent" list).
"""
from __future__ import annotations

from datetime import date
from typing import Any, Iterable

from django.conf import settings
from django.utils import timezone

from z_auth.models import User
from z_notification.services import notify

from .models import Member, ReportDelivery, ReportSchedule
from .report_pdf import report_filename, report_pdf
from .reports import build_report, clamp_period, clock, period_label, report_csv, report_title, size

MAX_EXTRA_EMAILS = 5
MAX_MANUAL_SENDS_PER_DAY = 20


def company_name(owner: User) -> str:
    company = getattr(owner, "owned_company", None)
    return (getattr(company, "name", "") or "").strip() or owner.email


def owner_name(owner: User) -> str:
    return (getattr(owner, "full_name", "") or "").strip()


def clean_emails(emails: Iterable[str] | None) -> list[str]:
    """Lower-cased, unique, in the order given."""
    out: list[str] = []
    for raw in emails or []:
        email = (raw or "").strip().lower()
        if email and email not in out:
            out.append(email)
    return out


def _duration(seconds: int) -> str:
    minutes = int(seconds or 0) // 60
    h, m = divmod(minutes, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def _report_url(report: dict[str, Any]) -> str:
    url = f"{settings.ZINWE_APP_URL}/app/reports?from={report['from']}&to={report['to']}"
    member = report.get("member")
    return f"{url}&member={member['id']}" if member else url


def _summary(report: dict[str, Any]) -> list[tuple[str, str]]:
    """The few numbers an email shows (label, value)."""
    t = report["totals"]
    days = t.get("person_days") or 0
    avg = t["worked_seconds"] // days if days else 0
    if report.get("member"):
        person = report["people"][0] if report["people"] else None
        rows = [
            ("Days worked", str(days)),
            ("Time worked", _duration(t["worked_seconds"])),
            ("Average a day", _duration(avg)),
            ("On breaks", _duration(t["break_seconds"])),
            ("Usual start", clock(person["first_in"]) if person else "-"),
            ("Usual finish", clock(person["last_out"]) if person else "-"),
        ]
        if t["active_seconds"]:
            rows.append(("Active on the PC", _duration(t["active_seconds"])))
        return rows
    rows = [
        ("People who worked", str(t["people_worked"])),
        ("Time worked", _duration(t["worked_seconds"])),
        ("Average a person-day", _duration(avg)),
        ("On breaks", _duration(t["break_seconds"])),
    ]
    if t["active_seconds"]:
        rows.append(("Active on the PC", _duration(t["active_seconds"])))
    if t["data_bytes"]:
        rows.append(("Internet data", size(t["data_bytes"])))
    rows.append(("Alerts", str(t["alerts_all"])))
    if t["to_review"]:
        rows.append(("Waiting for your OK", str(t["to_review"])))
    return rows


def _has_activity(report: dict[str, Any]) -> bool:
    t = report["totals"]
    return bool(t["person_days"] or t["active_seconds"] or t["alerts_all"])


def _attachments(report: dict[str, Any], company: str, include_csv: bool) -> list[tuple[str, bytes, str]]:
    files = [(report_filename(report), report_pdf(report, company), "application/pdf")]
    if include_csv:
        files.append((report_filename(report, "csv"), report_csv(report, company).encode("utf-8-sig"), "text/csv"))
    return files


def send_report(
    owner: User,
    start: date,
    end: date,
    *,
    member: Member | None = None,
    to_owner: bool = True,
    to_members: bool = False,
    emails: Iterable[str] | None = None,
    include_csv: bool = False,
    schedule: ReportSchedule | None = None,
) -> ReportDelivery:
    """Build and email the report for `start`..`end`; returns the recorded delivery."""
    start, end = clamp_period(owner, start, end)
    company = company_name(owner)
    sent: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []

    report = build_report(owner, start, end, member=member)
    readers = clean_emails([owner.email] if to_owner else [])
    readers += [e for e in clean_emails(emails)[:MAX_EXTRA_EMAILS] if e not in readers]
    if readers:
        files = _attachments(report, company, include_csv)
        context = {
            "title": report_title(report),
            "period": period_label(report),
            "company": company,
            "summary": _summary(report),
            "people": [
                {"name": p["name"], "worked": _duration(p["worked_seconds"]), "days": p["days_worked"]}
                for p in report["people"][:8]
            ] if not report.get("member") else [],
            "more_people": max(0, len(report["people"]) - 8) if not report.get("member") else 0,
            "report_url": _report_url(report),
            "scheduled": schedule is not None,
            "schedule_label": schedule.get_frequency_display() if schedule else "",
        }
        for email in readers:
            is_owner = email == owner.email.lower()
            result = notify(
                "email", email, "report_shared",
                {**context, "is_owner": is_owner, "first_name": (owner_name(owner).split() or [""])[0] if is_owner else ""},
                ref=schedule or owner, attachments=files,
                reply_to=None if is_owner else [owner.email],
            )
            (sent if result is not None and result.status == "sent" else failed).append(email)

    if to_members:
        people = [member] if member is not None else list(
            Member.objects.filter(owner=owner, is_active=True, pk__in=[p["member_id"] for p in report["people"] if p["member_id"]])
        )
        for person in people:
            if not person.is_active or not person.email:
                continue
            own = report if member is not None else build_report(owner, start, end, member=person)
            if not _has_activity(own):
                skipped.append(person.email)
                continue
            result = notify(
                "email", person.email, "report_member",
                {
                    "first_name": (person.name.split() or [""])[0],
                    "company": company,
                    "owner_name": owner_name(owner),
                    "period": period_label(own),
                    "summary": _summary(own),
                    "scheduled": schedule is not None,
                },
                ref=schedule or owner,
                attachments=_attachments(own, company, include_csv=False),
                reply_to=[owner.email],
            )
            (sent if result is not None and result.status == "sent" else failed).append(person.email)

    if not sent and not failed:
        state = ReportDelivery.Status.SKIPPED
        detail = "Nobody to send it to: no one had anything recorded." if skipped else "No recipients were chosen."
    elif failed and not sent:
        state, detail = ReportDelivery.Status.FAILED, f"Couldn't send to {', '.join(failed[:3])}."
    elif failed:
        state, detail = ReportDelivery.Status.PARTIAL, f"Sent to {len(sent)}; couldn't send to {', '.join(failed[:3])}."
    else:
        state = ReportDelivery.Status.SENT
        detail = f"Sent to {len(sent)} {'address' if len(sent) == 1 else 'addresses'}."
    if skipped and (sent or failed):
        detail += f" {len(skipped)} with nothing recorded skipped."
    return ReportDelivery.objects.create(
        owner=owner, schedule=schedule, member=member, period_from=start, period_to=end,
        recipients=sent + failed, status=state, detail=detail[:300],
    )


def manual_sends_today(owner: User) -> int:
    since = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    return ReportDelivery.objects.filter(owner=owner, schedule__isnull=True, created_at__gte=since).count()
