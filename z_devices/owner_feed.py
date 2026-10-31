"""
FR-15: what's new for the owner since their dashboard last looked, for browser notifications: member chat replies,
alerts, requests waiting for their OK (breaks, log-outs) and tasks a member needs help with or finished (FR-20). The dashboard polls `feed(owner, since)` and shows
each item as a notification; `NotificationPrefs` says which kinds the owner wants.

Web push (z_devices/web_push.py) tells the owner the same things the moment they happen, through `items_for()`. Both go
through `KINDS` below, one rows function (which rows this owner gets: NotificationPrefs + plan) and one item function
(what the notification says) per kind, so a push and the poll can't disagree. The item id is the notification's tag on
both paths, so the browser shows one notification, not two.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, NamedTuple

from django.db.models import QuerySet
from django.utils import timezone

from z_auth import entitlements as plan
from z_auth.entitlements import Entitlements, entitlements_for
from z_auth.models import User

from .events import build_title
from .models import DeviceEvent, Message, NotificationPrefs, TaskActivity, WorkLog

# A browser that was asleep for a day shouldn't pop a day of notifications: look back at most this far.
MAX_LOOKBACK = timedelta(hours=1)
MAX_ITEMS = 20

SEVERITIES_FOR = {
    NotificationPrefs.AlertLevel.ALL: {"info", "warning", "critical"},
    NotificationPrefs.AlertLevel.WARNING: {"warning", "critical"},
    NotificationPrefs.AlertLevel.CRITICAL: {"critical"},
    NotificationPrefs.AlertLevel.OFF: set(),
}

# FR-20: what a member does on a task that the owner hears about.
TASK_ACTIONS = ("help", "done")


def prefs_for(owner: User) -> NotificationPrefs:
    """The owner's settings (unsaved defaults when they never changed them)."""
    return NotificationPrefs.objects.filter(owner=owner).first() or NotificationPrefs(owner=owner)


def _short(text: str, limit: int = 140) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _chat_rows(owner: User, prefs: NotificationPrefs, ent: Entitlements) -> QuerySet:
    if not (prefs.chat and ent.has(plan.TEAM_CHAT)):
        return Message.objects.none()
    return Message.objects.filter(owner=owner, sender=Message.Sender.MEMBER).select_related("member")


def _chat_item(m: Message) -> dict[str, Any]:
    return {
        "id": f"chat-{m.pk}", "kind": "chat", "title": f"{m.member.name} wrote",
        "body": _short(m.body), "at": m.created_at.isoformat(), "link": f"/app/chat?member={m.member_id}",
    }


def _alert_rows(owner: User, prefs: NotificationPrefs, ent: Entitlements) -> QuerySet:
    levels = SEVERITIES_FOR.get(prefs.alerts, set())
    if not levels:
        return DeviceEvent.objects.none()
    from .services import _locked_kinds, confirmation_q

    return (
        DeviceEvent.objects.filter(device__owner=owner, severity__in=sorted(levels))
        .exclude(_locked_kinds("type", ent))
        .exclude(confirmation_q())
        .select_related("device")
    )


def _alert_item(e: DeviceEvent) -> dict[str, Any]:
    return {
        "id": f"alert-{e.pk}", "kind": "alert", "severity": e.severity,
        "title": "Critical alert" if e.severity == "critical" else "New alert",
        "body": _short(build_title(e.type, e.data, e.device.hostname)),
        "at": e.received_at.isoformat(), "link": "/app/alerts",
    }


def _approval_rows(owner: User, prefs: NotificationPrefs, ent: Entitlements) -> QuerySet:
    if not prefs.approvals:
        return WorkLog.objects.none()
    return WorkLog.objects.filter(owner=owner, approval=WorkLog.Approval.PENDING).select_related("member", "device")


def _approval_item(w: WorkLog) -> dict[str, Any]:
    who = w.member.name if w.member else w.device.hostname
    what = "log out" if w.state == "ended" else "take a break"
    return {
        "id": f"approval-{w.pk}", "kind": "approval", "title": f"{who} wants to {what}",
        "body": _short(" · ".join(p for p in [w.reason, w.note] if p)) or "Waiting for your OK on Activity.",
        "at": w.created_at.isoformat(), "link": f"/app/activity?member={w.member_id}" if w.member_id else "/app/activity",
    }


def _task_rows(owner: User, prefs: NotificationPrefs, ent: Entitlements) -> QuerySet:
    if not (prefs.tasks and ent.has(plan.TEAM_CHAT)):
        return TaskActivity.objects.none()
    # FR-20: a member asked for help on a task, or finished one (it now waits for the owner's OK).
    return TaskActivity.objects.filter(task__owner=owner, by="member", action__in=TASK_ACTIONS).select_related("task__member")


def _task_item(a: TaskActivity) -> dict[str, Any]:
    who = a.task.member.name
    help_asked = a.action == "help"
    return {
        "id": f"task-{a.pk}", "kind": "task",
        "title": f"{who} needs help with a task" if help_asked else f"{who} finished a task",
        "body": _short(f"{a.task.title}: {a.note}" if a.note else a.task.title),
        "at": a.at.isoformat(), "link": f"/app/tasks?task={a.task_id}",
    }


class _Kind(NamedTuple):
    rows: Callable[[User, NotificationPrefs, Entitlements], QuerySet]  # what this owner gets, by prefs and plan
    time_field: str
    item: Callable[[Any], dict[str, Any]]


KINDS: dict[str, _Kind] = {
    "chat": _Kind(_chat_rows, "created_at", _chat_item),
    "alert": _Kind(_alert_rows, "received_at", _alert_item),
    "approval": _Kind(_approval_rows, "created_at", _approval_item),
    "task": _Kind(_task_rows, "at", _task_item),
}


def feed(owner: User, since: datetime | None, now: datetime | None = None) -> dict[str, Any]:
    """{now, items: [{id, kind, title, body, at, link}]} newest first; nothing at all on the first call (no since)."""
    now = now or timezone.now()
    if since is None:
        return {"now": now.isoformat(), "items": []}
    since = max(since, now - MAX_LOOKBACK)
    prefs = prefs_for(owner)
    ent = entitlements_for(owner)
    items: list[dict[str, Any]] = []
    for kind in KINDS.values():
        recent = kind.rows(owner, prefs, ent).filter(**{f"{kind.time_field}__gt": since}).order_by(f"-{kind.time_field}")
        items.extend(kind.item(row) for row in recent[:MAX_ITEMS])

    items.sort(key=lambda i: i["at"], reverse=True)
    return {"now": now.isoformat(), "items": items[:MAX_ITEMS]}


def items_for(owner: User, kind: str, ids: Iterable[int]) -> list[dict[str, Any]]:
    """These rows as feed items, oldest first: only the ones `feed()` would show this owner (prefs, plan), so maybe none."""
    entry = KINDS[kind]
    rows = entry.rows(owner, prefs_for(owner), entitlements_for(owner)).filter(pk__in=list(ids))
    return [entry.item(row) for row in rows.order_by(entry.time_field)]
