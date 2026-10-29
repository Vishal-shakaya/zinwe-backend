"""
FR-15: what's new for the owner since their dashboard last looked, for browser notifications: member chat replies,
alerts, requests waiting for their OK (breaks, log-outs) and tasks a member needs help with or finished (FR-20). The dashboard polls `feed(owner, since)` and shows
each item as a notification; `NotificationPrefs` says which kinds the owner wants.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone

from z_auth import entitlements as plan
from z_auth.entitlements import entitlements_for
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


def prefs_for(owner: User) -> NotificationPrefs:
    """The owner's settings (unsaved defaults when they never changed them)."""
    return NotificationPrefs.objects.filter(owner=owner).first() or NotificationPrefs(owner=owner)


def _short(text: str, limit: int = 140) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def feed(owner: User, since: datetime | None, now: datetime | None = None) -> dict[str, Any]:
    """{now, items: [{id, kind, title, body, at, link}]} newest first; nothing at all on the first call (no since)."""
    now = now or timezone.now()
    if since is None:
        return {"now": now.isoformat(), "items": []}
    since = max(since, now - MAX_LOOKBACK)
    prefs = prefs_for(owner)
    ent = entitlements_for(owner)
    items: list[dict[str, Any]] = []

    if prefs.chat and ent.has(plan.TEAM_CHAT):
        replies = (
            Message.objects.filter(owner=owner, sender=Message.Sender.MEMBER, created_at__gt=since)
            .select_related("member")
            .order_by("-created_at")[:MAX_ITEMS]
        )
        for m in replies:
            items.append({
                "id": f"chat-{m.pk}", "kind": "chat", "title": f"{m.member.name} wrote",
                "body": _short(m.body), "at": m.created_at.isoformat(), "link": f"/app/chat?member={m.member_id}",
            })

    levels = SEVERITIES_FOR.get(prefs.alerts, set())
    if levels:
        from .services import _locked_kinds, confirmation_q

        events = (
            DeviceEvent.objects.filter(device__owner=owner, received_at__gt=since, severity__in=sorted(levels))
            .exclude(_locked_kinds("type", ent))
            .exclude(confirmation_q())
            .select_related("device")
            .order_by("-received_at")[:MAX_ITEMS]
        )
        for e in events:
            items.append({
                "id": f"alert-{e.pk}", "kind": "alert", "severity": e.severity,
                "title": "Critical alert" if e.severity == "critical" else "New alert",
                "body": _short(build_title(e.type, e.data, e.device.hostname)),
                "at": e.received_at.isoformat(), "link": "/app/alerts",
            })

    if prefs.approvals:
        waiting = (
            WorkLog.objects.filter(owner=owner, approval=WorkLog.Approval.PENDING, created_at__gt=since)
            .select_related("member", "device")
            .order_by("-created_at")[:MAX_ITEMS]
        )
        for w in waiting:
            who = w.member.name if w.member else w.device.hostname
            what = "log out" if w.state == "ended" else "take a break"
            items.append({
                "id": f"approval-{w.pk}", "kind": "approval", "title": f"{who} wants to {what}",
                "body": _short(" · ".join(p for p in [w.reason, w.note] if p)) or "Waiting for your OK on Activity.",
                "at": w.created_at.isoformat(), "link": f"/app/activity?member={w.member_id}" if w.member_id else "/app/activity",
            })

    if prefs.tasks and ent.has(plan.TEAM_CHAT):
        # FR-20: a member asked for help on a task, or finished one (it now waits for the owner's OK).
        moves = (
            TaskActivity.objects.filter(
                task__owner=owner, by="member", action__in=("help", "done"), at__gt=since
            )
            .select_related("task__member")
            .order_by("-at")[:MAX_ITEMS]
        )
        for a in moves:
            who = a.task.member.name
            help_asked = a.action == "help"
            items.append({
                "id": f"task-{a.pk}", "kind": "task",
                "title": f"{who} needs help with a task" if help_asked else f"{who} finished a task",
                "body": _short(f"{a.task.title}: {a.note}" if a.note else a.task.title),
                "at": a.at.isoformat(), "link": f"/app/tasks?task={a.task_id}",
            })

    items.sort(key=lambda i: i["at"], reverse=True)
    return {"now": now.isoformat(), "items": items[:MAX_ITEMS]}
