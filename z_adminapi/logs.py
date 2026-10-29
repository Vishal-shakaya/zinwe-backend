"""
FR-37 Logs: one timeline of what happened to this environment, for the admin app's SYSTEM > Logs page.

Rows come from four real sources, merged newest first:
  - `OpsEvent`           deploys, rollbacks, backups (+ off-site copy), API starts, 500s (z_analytics.ops)
  - audit `Event`s       "admin.*" = staff actions, with who did it (FIX-007); "admin.release_updated" -> agent
  - `AgentRelease`       each Windows agent release published from the admin (agent)
  - `ManualPayment`      payments staff approved or rejected (staff; the audit trail has no row for these)

Everything in the range is read (the range is at most 30 days and staff/ops events are few), filtered by text and
type in Python against the rendered row, counted per type, then capped at `limit` rows.
"""
from __future__ import annotations

import os
import shutil
import time
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import connection
from django.utils import timezone

from z_analytics.models import Event, OpsEvent
from z_analytics.ops import environment, release_info

RANGES: dict[str, timedelta] = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
DEFAULT_RANGE = "7d"
TYPES = list(OpsEvent.Type.values)
DEFAULT_LIMIT = 300
MAX_LIMIT = 1000
# Per-source safety cap (a 30-day range of a busy environment); rows past it are dropped oldest first.
SOURCE_CAP = 5000

AUDIT = "Admin audit log"
IST = ZoneInfo("Asia/Kolkata")

# Process start, for the status card ("API up since").
STARTED_AT = timezone.now()


class LogsQueryError(ValueError):
    pass


# --- rendering ------------------------------------------------------------------


def _row(
    *, id: str, at: datetime, type: str, label: str, env: str, what: str, who: str, result: str = "ok",
    result_label: str = "", details: list[list[str]] | None = None, steps: list[list[str]] | None = None,
    unresolved: bool = False, source: str, ops_id: int | None = None,
) -> dict[str, Any]:
    return {
        "id": id, "at": at.isoformat(), "type": type, "label": label, "env": env, "what": what, "who": who or "system",
        "result": result, "result_label": result_label, "details": details or [], "steps": steps or [],
        "unresolved": unresolved, "source": source, "ops_id": ops_id,
    }


def ops_row(e: OpsEvent) -> dict[str, Any]:
    details = [list(map(str, d)) for d in (e.details or [])]
    if e.resolved_at and e.result == OpsEvent.Result.FAIL:
        details.append(["Resolved", f"{timezone.localtime(e.resolved_at, IST):%d %b %H:%M} IST · {e.resolved_by or 'staff'}"])
    return _row(
        id=f"ops-{e.pk}", at=e.at, type=e.type, label=e.label, env=e.env, what=e.what, who=e.who, result=e.result,
        result_label=e.result_label, details=details, steps=[list(map(str, s)) for s in (e.steps or [])],
        unresolved=e.unresolved, source="ops", ops_id=e.pk,
    )


def _humanize(name: str) -> str:
    text = name.split(".", 1)[-1].replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def _fields(props: dict[str, Any]) -> str:
    names = {"is_active": "on/off", "published_at": "publish date", "body_html": "text", "cta_href": "button link",
             "cta_label": "button text", "plan_codes": "plans", "starts_at": "start", "ends_at": "end"}
    return ", ".join(names.get(f, f.replace("_", " ")) for f in props.get("fields") or [])


def _audit_what(e: Event, emails: dict[str, str], titles: dict[str, str]) -> tuple[str, str, list[list[str]]]:
    """(type, what, details) for one "admin.*" / "agent.*" audit event."""
    p = e.properties or {}
    action = e.name.split(".", 1)[-1]
    customer = p.get("email") or p.get("to") or emails.get(str(p.get("customer_id") or "")) or (
        emails.get(e.ref_id, "") if e.ref_type == "z_auth.user" else ""
    )
    customer = customer or (f"customer #{p['customer_id']}" if p.get("customer_id") else "a customer")
    title = p.get("title") or titles.get(f"{e.ref_type}:{e.ref_id}") or titles.get(f"z_auth.announcement:{p.get('id')}", "")
    details: list[list[str]] = []
    kind = OpsEvent.Type.STAFF

    if action == "customer_deleted":
        what = f"Deleted customer {customer}"
        details.append(["Removed", "The account, its company, PCs and activity"])
    elif action == "customer_blocked":
        what = f"Blocked customer {customer}"
        if p.get("reason"):
            details.append(["Reason", str(p["reason"])])
        details.append(["Emailed them", "Yes" if p.get("emailed") else "No"])
    elif action == "customer_unblocked":
        what = f"Unblocked customer {customer}"
    elif action in {"remark_saved", "remark_cleared"}:
        what = f"{'Saved' if action == 'remark_saved' else 'Cleared'} remark · {customer}"
        details.append(["Remark", "Kept on the customer page, not in the log"])
    elif action == "payment_link_set":
        what = f"Set payment link · {customer}" if p.get("payment_link") else f"Removed payment link · {customer}"
        if p.get("payment_link"):
            details.append(["Link", str(p["payment_link"])])
    elif action == "password_set":
        what = f"Set a new password · {customer}"
        details.append(["Emailed them", "Yes" if p.get("emailed") else "No"])
    elif action == "password_reset_sent":
        what = f"Sent a password reset link · {customer}"
    elif action == "plan_changed":
        what = f"Changed plan · {customer} · {p.get('from') or 'none'} → {p.get('to') or '?'}"
        details.append(["Emailed them", "Yes" if p.get("emailed") else "No"])
    elif action == "email_sent":
        what = f"Emailed {customer}"
        if p.get("subject"):
            details.append(["Subject", str(p["subject"])])
    elif action == "pricing_changed":
        what = f"Changed prices · {p.get('plan') or 'a'} plan"
        if p.get("fields"):
            details.append(["Changed", _fields(p)])
    elif action.startswith("announcement_"):
        verb = {"created": "Published", "updated": "Edited", "deleted": "Deleted"}.get(action.rsplit("_", 1)[-1], "Changed")
        what = f"{verb} announcement · {title or '#' + str(p.get('id', '?'))}"
        if p.get("audience"):
            details.append(["Shown to", str(p["audience"])])
        if p.get("placement"):
            details.append(["Where", str(p["placement"])])
        if p.get("fields"):
            details.append(["Changed", _fields(p)])
    elif action.startswith("release_note_"):
        verb = {"created": "Wrote", "updated": "Edited", "deleted": "Deleted", "published": "Published",
                "unpublished": "Unpublished"}.get(action.rsplit("_", 1)[-1], "Changed")
        what = f"{verb} release note · version {p.get('version') or '?'}"
        if p.get("fields") and verb == "Edited":
            details.append(["Changed", _fields(p)])
    elif action.startswith("blog_article_"):
        verb = {"created": "Wrote", "updated": "Edited", "deleted": "Deleted", "published": "Published",
                "unpublished": "Unpublished"}.get(action.rsplit("_", 1)[-1], "Changed")
        what = f"{verb} blog post · {title or '#' + str(p.get('id', '?'))}"
        if p.get("fields"):
            details.append(["Changed", _fields(p)])
    elif action == "newsletter_queued":
        what = f"Queued newsletter · {title or 'an article'}"
    elif action == "newsletter_subscriber_erased":
        what = "Erased a newsletter subscriber (their address is gone)"
    elif action == "log_resolved":
        what = f"Marked as resolved · {p.get('what') or 'a log entry'}"
    elif action == "release_updated":
        kind = OpsEvent.Type.AGENT
        changes = []
        for f in p.get("fields") or []:
            changes.append({"required": "required/optional", "is_active": "offered/withdrawn", "notes": "notes"}.get(f, f))
        what = f"Changed Windows agent {p.get('version') or '?'} · {', '.join(changes) or 'edited'}"
    else:
        what = _humanize(e.name)
    return kind, what, details


def audit_rows(since: datetime, env: str) -> list[dict[str, Any]]:
    events = list(
        Event.objects.filter(created_at__gte=since, name__startswith="admin.").order_by("-created_at", "-id")[:SOURCE_CAP]
    )
    user_ids = {e.ref_id for e in events if e.ref_type == "z_auth.user" and e.ref_id}
    user_ids |= {str(e.properties.get("customer_id")) for e in events if (e.properties or {}).get("customer_id")}
    from z_auth.models import Announcement, User

    emails = {str(pk): email for pk, email in User.objects.filter(pk__in=[i for i in user_ids if i.isdigit()]).values_list("pk", "email")}
    ann_ids = {e.ref_id for e in events if e.ref_type == "z_auth.announcement" and e.ref_id}
    ann_ids |= {str(e.properties.get("id")) for e in events if e.name.startswith("admin.announcement_") and (e.properties or {}).get("id")}
    titles = {
        f"z_auth.announcement:{pk}": t
        for pk, t in Announcement.objects.filter(pk__in=[i for i in ann_ids if i.isdigit()]).values_list("pk", "title")
    }
    rows = []
    for e in events:
        kind, what, details = _audit_what(e, emails, titles)
        details.append(["Source", AUDIT])
        rows.append(_row(
            id=f"evt-{e.pk}", at=e.created_at, type=kind, label="Agent release" if kind == "agent" else "Staff action",
            env=env, what=what, who=e.actor_email or "system", details=details, source="audit",
        ))
    return rows


def release_rows(since: datetime, env: str) -> list[dict[str, Any]]:
    from z_devices.models import AgentRelease

    releases = list(AgentRelease.objects.filter(published_at__gte=since).select_related("published_by")[:SOURCE_CAP])
    # Required / optional as published (the audit event has it); a later edit shows as its own row.
    published = {
        e.ref_id: bool((e.properties or {}).get("required"))
        for e in Event.objects.filter(name="agent.release_published", ref_type="z_devices.agentrelease",
                                      ref_id__in=[str(r.pk) for r in releases])
    }
    rows = []
    for r in releases:
        required = published.get(str(r.pk), r.required)
        details = [["Update now", "Required · older PCs pause until they update" if r.required else "Optional · the person agrees in the tray app"]]
        if r.size:
            details.append(["Installer", f"{r.size / 1_048_576:.1f} MB · uploaded"])
        elif r.download_url:
            details.append(["Installer", f"Hosted at {r.download_url}"])
        details.append(["Offered now", "Yes" if r.is_active else "No (withdrawn)"])
        if r.notes:
            details.append(["Notes", r.notes[:300]])
        details.append(["Source", "Releases page"])
        rows.append(_row(
            id=f"rel-{r.pk}", at=r.published_at, type=OpsEvent.Type.AGENT, label="Agent release", env=env,
            what=f"Published Windows agent {r.version} · {'Required' if required else 'Optional'}",
            who=r.published_by.email if r.published_by else "system", details=details, source="release",
        ))
    return rows


def _money(amount: Decimal | None, currency: str) -> str:
    if amount is None:
        return ""
    if currency == "USD":
        return f"${amount:,.2f}"
    return f"₹{amount:,.0f}" if amount == amount.to_integral_value() else f"₹{amount:,.2f}"


def payment_rows(since: datetime, env: str) -> list[dict[str, Any]]:
    from z_auth.models import ManualPayment

    qs = (
        ManualPayment.objects.filter(reviewed_at__gte=since, reviewed_by__isnull=False)
        .exclude(status=ManualPayment.Status.SUBMITTED)
        .select_related("reviewed_by", "subscription__owner", "subscription__company", "subscription__plan")
        .order_by("-reviewed_at")[:SOURCE_CAP]
    )
    rows = []
    for pay in qs:
        sub = pay.subscription
        name = (sub.company.name if sub.company_id and sub.company else "") or sub.owner.email
        approved = pay.status == ManualPayment.Status.VERIFIED
        money = _money(pay.amount_inr, pay.currency)
        details = [["Customer", f"{name} ({sub.owner.email})" if name != sub.owner.email else name],
                   ["Payment", f"{pay.get_method_display()} · {money}"], ["Plan", sub.plan.name]]
        if approved and pay.staff_reference:
            details.append(["Reference checked", pay.staff_reference])
        if not approved and pay.rejection_reason:
            details.append(["Reason", pay.rejection_reason[:300]])
        details.append(["Source", "Payment review"])
        rows.append(_row(
            id=f"pay-{pay.pk}", at=pay.reviewed_at, type=OpsEvent.Type.STAFF, label="Staff action", env=env,
            what=f"{'Approved' if approved else 'Rejected'} payment · {name} · {money} ({sub.plan.name})",
            who=pay.reviewed_by.email, details=details, source="payment",
        ))
    return rows


# --- query ----------------------------------------------------------------------


def _in_type(row: dict[str, Any], wanted: str) -> bool:
    if wanted == "error":  # the board's "Errors" chip: errors AND every failed job
        return row["type"] == "error" or row["result"] == "fail"
    return row["type"] == wanted


def _haystack(row: dict[str, Any]) -> str:
    parts = [row["what"], row["who"], row["label"], row["env"], row["result_label"]]
    parts += [" ".join(d) for d in row["details"]] + [" ".join(s) for s in row["steps"]]
    return " ".join(parts).lower()


def log_rows(range_key: str | None = None, type_key: str | None = None, q: str | None = None,
             limit: int | str | None = None) -> dict[str, Any]:
    range_key = range_key or DEFAULT_RANGE
    if range_key not in RANGES:
        raise LogsQueryError(f"Unknown range '{range_key}'. Use {', '.join(RANGES)}.")
    type_key = type_key or "all"
    if type_key != "all" and type_key not in TYPES:
        raise LogsQueryError(f"Unknown type '{type_key}'. Use all, {', '.join(TYPES)}.")
    try:
        cap = DEFAULT_LIMIT if limit in (None, "") else max(1, min(MAX_LIMIT, int(limit)))
    except (TypeError, ValueError) as exc:
        raise LogsQueryError("limit must be a number.") from exc

    now = timezone.now()
    since = now - RANGES[range_key]
    env = environment()
    rows = [ops_row(e) for e in OpsEvent.objects.filter(at__gte=since)[:SOURCE_CAP]]
    rows += audit_rows(since, env) + release_rows(since, env) + payment_rows(since, env)

    needle = " ".join((q or "").lower().split())
    if needle:
        rows = [r for r in rows if needle in _haystack(r)]
    counts = {"all": len(rows), **{t: sum(1 for r in rows if _in_type(r, t)) for t in TYPES}}
    if type_key != "all":
        rows = [r for r in rows if _in_type(r, type_key)]
    # Same instant (Windows clocks tick in ~15 ms): the later row of a source has the higher id.
    rows.sort(key=lambda r: (datetime.fromisoformat(r["at"]), int(r["id"].rsplit("-", 1)[1])), reverse=True)
    return {
        "env": env, "range": range_key, "type": type_key, "q": q or "", "now": now.isoformat(),
        "counts": counts, "total": len(rows), "truncated": len(rows) > cap, "rows": rows[:cap],
    }


# --- status ---------------------------------------------------------------------


def _cpu_percent(sample: float = 0.15) -> float | None:
    """Busy share of all CPUs over a short sample, from /proc/stat (Linux). None elsewhere."""
    def read() -> tuple[int, int] | None:
        try:
            with open("/proc/stat", encoding="ascii") as fh:
                parts = fh.readline().split()
        except OSError:
            return None
        if not parts or parts[0] != "cpu":
            return None
        values = [int(v) for v in parts[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return sum(values), idle

    first = read()
    if first is None:
        return None
    time.sleep(sample)
    second = read()
    if second is None:
        return None
    total, idle = second[0] - first[0], second[1] - first[1]
    return round(100 * (total - idle) / total, 1) if total > 0 else None


def _memory_percent() -> float | None:
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                info[key] = int(rest.split()[0])
        total, available = info["MemTotal"], info["MemAvailable"]
    except (OSError, KeyError, ValueError, IndexError):
        return None
    return round(100 * (total - available) / total, 1) if total else None


def _booted_at() -> str | None:
    try:
        with open("/proc/uptime", encoding="ascii") as fh:
            seconds = float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return (timezone.now() - timedelta(seconds=seconds)).isoformat()


def _disk() -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(settings.BASE_DIR)
    except OSError:
        return {"disk": None, "disk_free_gb": None}
    return {"disk": round(100 * usage.used / usage.total, 1) if usage.total else None,
            "disk_free_gb": round(usage.free / 1_073_741_824, 1)}


def _database() -> dict[str, Any]:
    start = time.perf_counter()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # noqa: BLE001 -- "down" is the answer, not an error
        return {"ok": False, "engine": connection.vendor, "latency_ms": None}
    return {"ok": True, "engine": connection.vendor, "latency_ms": round((time.perf_counter() - start) * 1000, 1)}


def _latest(type_: str, env: str, **extra: Any) -> dict[str, Any] | None:
    row = OpsEvent.objects.filter(type=type_, env=env, **extra).first()
    return ops_row(row) if row else None


def status(host: str = "") -> dict[str, Any]:
    env = environment()
    release = release_info()
    open_rows = OpsEvent.objects.filter(unresolved=True)
    return {
        "env": env,
        "checked_at": timezone.now().isoformat(),
        "api": {
            "ok": True, "host": host, "version": settings.ZINWE_VERSION, "started_at": STARTED_AT.isoformat(),
            "release": release, "pid": os.getpid(),
        },
        "database": _database(),
        "server": {
            "cpu": _cpu_percent(), "memory": _memory_percent(), **_disk(), "booted_at": _booted_at(),
            "cores": os.cpu_count(),
        },
        # Nobody measures availability yet: the UI hides the uptime figure while it's null.
        "uptime_30d": None,
        "last_deploy": _latest(OpsEvent.Type.DEPLOY, env),
        "last_backup": _latest(OpsEvent.Type.BACKUP, env),
        "unresolved": open_rows.count(),
        "attention": [ops_row(e) for e in open_rows[:5]],
    }


def resolve(event: OpsEvent, staff_email: str) -> OpsEvent:
    event.unresolved = False
    event.resolved_at = timezone.now()
    event.resolved_by = staff_email
    event.save(update_fields=["unresolved", "resolved_at", "resolved_by"])
    return event
