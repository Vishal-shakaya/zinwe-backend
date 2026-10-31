"""
A member's own to-dos: written from their Zinwe window (device API), read by the owner, who can't change them.

Views stay thin; the merge rule lives here. The Windows app queues each change and may send it twice or late, so:
- `client_id` (the app's id, unique per member) makes every write idempotent.
- Last write wins by `updated_at`, the member's own change time: anything not newer than the row is ignored.
- A delete wins over an edit of the same age or older. An edit made after a delete brings the to-do back (Undo).
- A PC whose clock runs far ahead can't win for ever: a time beyond FUTURE_SLACK counts as now.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from z_auth.models import User

from .models import Device, MemberTodo

S = MemberTodo.Status

MAX_STEPS = 50
# The most one list holds: more than a member gets through in years, and a limit on what a PC is sent over the pipe.
LIST_LIMIT = 500
FUTURE_SLACK = timedelta(days=1)


def _when(at: datetime) -> datetime:
    now = timezone.now()
    return now if at > now + FUTURE_SLACK else at


# --- Reading ----------------------------------------------------------------


def todos_for_owner(owner: User, status: str = "all", member_id: int | None = None) -> QuerySet:
    """GET todos/ and members/<id>/todos/ (owner): open, done or all, newest change first. Deleted ones never show."""
    qs = MemberTodo.objects.filter(owner=owner, deleted_at__isnull=True)
    if status in (S.OPEN, S.DONE):
        qs = qs.filter(status=status)
    if member_id is not None:
        qs = qs.filter(member_id=member_id)
    return qs.select_related("member").order_by("-updated_at", "-id")[:LIST_LIMIT]


def todos_for_device(device: Device) -> QuerySet:
    """GET todos/ (device): the PC's member's to-dos, deleted ones too, so their other PCs learn of the delete."""
    if device.member_id is None:
        return MemberTodo.objects.none()
    qs = MemberTodo.objects.filter(member_id=device.member_id, owner_id=device.owner_id)
    return qs.select_related("member").order_by("-updated_at", "-id")[:LIST_LIMIT]


# --- Writing ----------------------------------------------------------------


def _locked(device: Device, client_id: str, defaults: dict[str, Any]) -> tuple[MemberTodo, bool]:
    return MemberTodo.objects.select_for_update().get_or_create(
        member_id=device.member_id, client_id=client_id, defaults={"owner_id": device.owner_id, **defaults}
    )


def _content(data: dict[str, Any], at: datetime, done_at: datetime | None) -> dict[str, Any]:
    done = data["status"] == S.DONE
    return {
        "title": data["title"],
        "due_date": data["due_date"],
        "steps": data["steps"],
        "status": data["status"],
        "done_at": (done_at or at) if done else None,
        "updated_at": at,
        "deleted_at": None,
    }


@transaction.atomic
def save_todo(device: Device, client_id: str, data: dict[str, Any]) -> tuple[MemberTodo, bool]:
    """PUT todos/<client_id>/. Returns (the row as it now stands, created). Not newer than the row: nothing changes."""
    at = _when(data["updated_at"])
    todo, created = _locked(device, client_id, _content(data, at, None))
    if created or at <= todo.updated_at:
        return todo, created
    for name, value in _content(data, at, todo.done_at).items():
        setattr(todo, name, value)
    todo.save()
    return todo, False


@transaction.atomic
def delete_todo(device: Device, client_id: str, updated_at: datetime) -> MemberTodo:
    """
    DELETE todos/<client_id>/. Soft: the row stays, marked deleted. An id the server never saw is kept as an empty
    deleted row (the to-do was made and deleted offline), so a late edit from another PC can't bring it to life.
    """
    at = _when(updated_at)
    todo, created = _locked(device, client_id, {"updated_at": at, "deleted_at": at})
    if created or at < todo.updated_at or (todo.deleted_at and at == todo.updated_at):
        return todo
    todo.deleted_at = at
    todo.updated_at = at
    todo.save(update_fields=["deleted_at", "updated_at"])
    return todo
