"""
FR-20 v2: tasks the owner hands a member, worked from the member's Zinwe window.

Contract: .claude/dev/planning/v1.3.1-fix-roadmap.md ("Tasks API contract") and .claude/dev/docs/15-api-reference.md.
Views stay thin; every rule about who may do what, and which status follows, lives here.

Status flow: sent -> seen -> accepted -> progress -> review -> approved; side states help and cancelled.
- The member (device) can: seen, accept, start, help, done, note (a note and/or ticking a step).
- The owner can: approve (from any open status: "Mark done" goes straight to approved), reopen (-> progress,
  from review / approved / cancelled / help), cancel (any open task), nudge (an open task the member still has
  to finish, once an hour). The member can't approve; the owner can't start; cancelled stays cancelled until
  the owner reopens it.
- Asking for the status a task already has is a no-op (the Windows app's outbox may send an action twice).
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Iterable

from django.db import transaction
from django.db.models import F, Prefetch, Q, QuerySet, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from z_analytics.services import track
from z_auth.models import User

from .models import Device, Member, Message, Task, TaskActivity, TaskStep

S = Task.Status
BY_OWNER = Task.By.OWNER
BY_MEMBER = Task.By.MEMBER

MAX_STEPS = 20
# The member's window keeps approved tasks this long, then they drop off its list.
DEVICE_APPROVED_DAYS = 7
NUDGE_EVERY = timedelta(hours=1)
OWNER_LIST_LIMIT = 300

OWNER_ACTIONS = ("approve", "reopen", "cancel", "nudge")
MEMBER_ACTIONS = ("seen", "accept", "start", "help", "done", "note")

# action -> (statuses it may start from, status it leads to). Same status = no-op (handled before this table).
_MEMBER_MOVES: dict[str, tuple[tuple[str, ...], str]] = {
    "seen": ((S.SENT,), S.SEEN),
    "accept": ((S.SENT, S.SEEN), S.ACCEPTED),
    "start": ((S.SENT, S.SEEN, S.ACCEPTED, S.HELP, S.REVIEW), S.PROGRESS),
    "help": ((S.SENT, S.SEEN, S.ACCEPTED, S.PROGRESS, S.HELP), S.HELP),
    "done": ((S.SENT, S.SEEN, S.ACCEPTED, S.PROGRESS, S.HELP), S.REVIEW),
}
_OWNER_MOVES: dict[str, tuple[tuple[str, ...], str]] = {
    "approve": ((S.SENT, S.SEEN, S.ACCEPTED, S.PROGRESS, S.HELP, S.REVIEW), S.APPROVED),
    "reopen": ((S.REVIEW, S.APPROVED, S.CANCELLED, S.HELP), S.PROGRESS),
    "cancel": ((S.SENT, S.SEEN, S.ACCEPTED, S.PROGRESS, S.HELP, S.REVIEW), S.CANCELLED),
}
# The activity line each action writes.
_ACTIVITY = {
    "seen": "seen", "accept": "accepted", "start": "started", "help": "help", "done": "done",
    "approve": "approved", "reopen": "reopened", "cancel": "cancelled", "nudge": "nudged",
}
NUDGEABLE = (S.SENT, S.SEEN, S.ACCEPTED, S.PROGRESS, S.HELP)


class TaskRefused(Exception):
    """The action isn't allowed for this task's status (or the request is incomplete): a 400 for `field`."""

    def __init__(self, message: str, field: str = "action") -> None:
        super().__init__(message)
        self.field = field
        self.message = message


# --- Reading ----------------------------------------------------------------


def with_details(qs: QuerySet) -> QuerySet:
    """Everything the Task JSON needs, in three queries however many tasks."""
    return qs.select_related("member").prefetch_related(
        Prefetch("steps", queryset=TaskStep.objects.order_by("position", "id")),
        Prefetch("activity", queryset=TaskActivity.objects.order_by("at", "id")),
    )


def tasks_for_owner(owner: User, status: str = "open", member_id: int | None = None) -> QuerySet:
    """GET tasks/ (owner): open = not approved/cancelled, done = approved or cancelled, all. Newest activity first."""
    qs = Task.objects.filter(owner=owner)
    if status == "open":
        qs = qs.exclude(status__in=Task.CLOSED)
    elif status == "done":
        qs = qs.filter(status__in=Task.CLOSED)
    if member_id is not None:
        qs = qs.filter(member_id=member_id)
    return with_details(qs.order_by("-updated_at", "-id"))[:OWNER_LIST_LIMIT]


def tasks_for_device(device: Device, now: Any = None) -> QuerySet:
    """GET tasks/ (device): the PC's member's open tasks + the ones approved in the last 7 days."""
    if device.member_id is None:
        return Task.objects.none()
    now = now or timezone.now()
    recent = Q(status=S.APPROVED, status_changed_at__gte=now - timedelta(days=DEVICE_APPROVED_DAYS))
    qs = Task.objects.filter(member_id=device.member_id, owner_id=device.owner_id).filter(
        ~Q(status__in=Task.CLOSED) | recent
    )
    return with_details(qs.order_by("-updated_at", "-id"))


def task_for_owner(owner: User, task_id: int) -> Task | None:
    return with_details(Task.objects.filter(owner=owner, pk=task_id)).first()


def task_for_device(device: Device, task_id: int) -> Task | None:
    """Only a task for this PC's own member (anything else is "not found")."""
    if device.member_id is None:
        return None
    return with_details(
        Task.objects.filter(pk=task_id, member_id=device.member_id, owner_id=device.owner_id)
    ).first()


def owner_needs_count(owner: User) -> int:
    """Open tasks waiting on the owner: a member asked for help, or finished and waits for the OK."""
    return Task.objects.filter(owner=owner, status__in=(S.HELP, S.REVIEW)).count()


# --- Writing ----------------------------------------------------------------


def _log(task: Task, by: str, action: str, note: str = "", at: Any = None) -> TaskActivity:
    return TaskActivity.objects.create(task=task, by=by, action=action, note=(note or "")[:1000], at=at or timezone.now())


def _move(task: Task, status: str, by: str, now: Any) -> None:
    task.status = status
    task.status_changed_at = now
    task.status_changed_by = by


def _clean_titles(values: Iterable[Any]) -> list[str]:
    return [t for t in (" ".join(str(v or "").split())[:200] for v in values) if t]


@transaction.atomic
def create_task(
    owner: User,
    member: Member,
    title: str,
    note: str = "",
    due_at: Any = None,
    priority: str = Task.Priority.NORMAL,
    steps: list[str] | None = None,
) -> Task:
    """
    POST members/<id>/tasks/: the task plus its chat line (kind "task"). The chat line carries the title and note,
    so a Zinwe window older than 1.3.1 shows it as an ordinary message.
    """
    if member.owner_id != owner.id:
        raise ValueError("Member does not belong to this owner.")
    if not member.is_active:
        raise TaskRefused("This person is switched off. Switch them back on to send a task.", field="member")
    message = Message.objects.create(
        owner=owner, member=member, sender=Message.Sender.OWNER, channel=Message.Channel.CHAT,
        kind=Message.Kind.TASK, title=title, body=note, severity=Message.Severity.INFO, read_by_owner=True,
    )
    now = timezone.now()
    task = Task.objects.create(
        owner=owner, member=member, message=message, title=title, note=note, due_at=due_at,
        priority=priority, status=S.SENT, status_changed_at=now, status_changed_by=BY_OWNER,
    )
    titles = _clean_titles(steps or [])[:MAX_STEPS]
    TaskStep.objects.bulk_create([TaskStep(task=task, title=t, position=i) for i, t in enumerate(titles)])
    _log(task, BY_OWNER, "created", at=now)
    transaction.on_commit(
        lambda: track(
            "task.created",
            {"member_id": member.id, "steps": len(titles), "priority": priority, "due": due_at is not None},
            actor_email=owner.email,
            ref=task,
        )
    )
    return task_for_owner(owner, task.pk)  # type: ignore[return-value]


def _replace_steps(task: Task, raw: list[Any]) -> None:
    """
    PATCH steps: a list of titles and/or {id?, title, done?}. A step keeps its tick when it is sent back by id, or by
    the same title; steps left out are removed.
    """
    existing = list(task.steps.all())
    by_id = {s.pk: s for s in existing}
    used: set[int] = set()
    keep: list[TaskStep] = []
    for position, item in enumerate(raw[:MAX_STEPS]):
        if isinstance(item, dict):
            title = " ".join(str(item.get("title") or "").split())[:200]
            step = by_id.get(item.get("id")) if isinstance(item.get("id"), int) else None
            done = item.get("done")
        else:
            title, step, done = " ".join(str(item or "").split())[:200], None, None
        if not title:
            continue
        if step is None or step.pk in used:
            step = next((s for s in existing if s.title == title and s.pk not in used), None)
        if step is None:
            step = TaskStep(task=task, title=title, done=bool(done))
        else:
            step.title = title
            if done is not None:
                step.done = bool(done)
            used.add(step.pk)
        step.position = position
        keep.append(step)
    task.steps.exclude(pk__in=[s.pk for s in keep if s.pk]).delete()
    for step in keep:
        step.save()


@transaction.atomic
def edit_task(owner: User, task: Task, data: dict[str, Any]) -> Task:
    """PATCH tasks/<id>/: title, note, due date, priority, steps. A cancelled task must be reopened first."""
    if task.status == S.CANCELLED:
        raise TaskRefused("This task is cancelled. Reopen it to change it.", field="status")
    fields: list[str] = []
    for name in ("title", "note", "due_at", "priority"):
        if name in data and getattr(task, name) != data[name]:
            setattr(task, name, data[name])
            fields.append(name)
    if "steps" in data:
        before = [(s.title, s.done) for s in task.steps.all()]
        _replace_steps(task, data["steps"])
        if [(s.title, s.done) for s in task.steps.all()] != before:
            fields.append("steps")
    if not fields:
        return task_for_owner(owner, task.pk)  # type: ignore[return-value]
    task.save()  # bumps updated_at even when only the steps changed
    if task.message_id and ({"title", "note"} & set(fields)):
        Message.objects.filter(pk=task.message_id).update(title=task.title, body=task.note)
    _log(task, BY_OWNER, "edited", ", ".join(fields))
    return task_for_owner(owner, task.pk)  # type: ignore[return-value]


def _same_status_noop(action: str, task: Task, table: dict[str, tuple[tuple[str, ...], str]]) -> bool:
    move = table.get(action)
    return move is not None and task.status == move[1] and action != "help"


@transaction.atomic
def owner_action(owner: User, task: Task, action: str, note: str = "") -> Task:
    """POST tasks/<id>/action/ (owner): approve, reopen, cancel or nudge."""
    task = Task.objects.select_for_update().get(pk=task.pk)
    now = timezone.now()
    note = (note or "").strip()
    if action == "nudge":
        if task.status not in NUDGEABLE:
            raise TaskRefused("Only a task they still have to finish can be nudged.")
        if task.nudged_at and now - task.nudged_at < NUDGE_EVERY:
            raise TaskRefused("You nudged them less than an hour ago.")
        task.nudged_at = now
        task.save(update_fields=["nudged_at", "updated_at"])
        _log(task, BY_OWNER, "nudged", note, at=now)
        return task_for_owner(owner, task.pk)  # type: ignore[return-value]
    if _same_status_noop(action, task, _OWNER_MOVES):
        return task_for_owner(owner, task.pk)  # type: ignore[return-value]
    allowed_from, target = _OWNER_MOVES[action]
    if task.status not in allowed_from:
        raise TaskRefused(_owner_refusal(action, task.status))
    _move(task, target, BY_OWNER, now)
    task.save()
    _log(task, BY_OWNER, _ACTIVITY[action], note, at=now)
    transaction.on_commit(lambda: track(f"task.{_ACTIVITY[action]}", {"by": "owner"}, actor_email=owner.email, ref=task))
    return task_for_owner(owner, task.pk)  # type: ignore[return-value]


def _owner_refusal(action: str, status: str) -> str:
    if status == S.CANCELLED:
        return "This task is cancelled. Reopen it first."
    if action == "reopen":
        return "This task is still open."
    if action == "cancel" and status == S.APPROVED:
        return "This task is already approved. Reopen it first."
    return "That can't be done to this task now."


@transaction.atomic
def member_action(
    device: Device,
    task: Task,
    action: str,
    note: str = "",
    step_id: int | None = None,
    step_done: bool | None = None,
) -> Task:
    """POST tasks/<id>/action/ (device): seen, accept, start, help, done, or note (a note and/or ticking a step)."""
    task = Task.objects.select_for_update().get(pk=task.pk)
    now = timezone.now()
    note = (note or "").strip()

    if action == "note":
        if not task.is_open:
            raise TaskRefused("This task is closed.")
        if step_id is None and not note:
            raise TaskRefused("Write a note or pick a step.", field="note")
        if step_id is not None:
            step = task.steps.filter(pk=step_id).first()
            if step is None:
                raise TaskRefused("That step isn't on this task.", field="step_id")
            done = (not step.done) if step_done is None else bool(step_done)
            if step.done != done:
                step.done = done
                step.save(update_fields=["done"])
                _log(task, BY_MEMBER, "step_done" if done else "step_undone", step.title, at=now)
        if note:
            _log(task, BY_MEMBER, "note", note, at=now)
        task.save(update_fields=["updated_at"])
        return task_for_device(device, task.pk)  # type: ignore[return-value]

    if action == "help" and not note:
        raise TaskRefused("Say what you need help with.", field="note")
    if _same_status_noop(action, task, _MEMBER_MOVES) or (action == "seen" and task.status != S.SENT):
        return task_for_device(device, task.pk)  # type: ignore[return-value]
    allowed_from, target = _MEMBER_MOVES[action]
    if task.status not in allowed_from:
        raise TaskRefused(
            "This task is closed." if not task.is_open else "That can't be done to this task now."
        )
    _move(task, target, BY_MEMBER, now)
    if action == "help":
        task.help_note = note[:1000]
    task.save()
    _log(task, BY_MEMBER, _ACTIVITY[action], note, at=now)
    if action == "seen" and task.message_id:
        Message.objects.filter(pk=task.message_id, read_by_member=False).update(
            read_by_member=True, read_by_member_at=now, delivered_at=Coalesce(F("delivered_at"), Value(now))
        )
    member_email = task.member.email
    transaction.on_commit(lambda: track(f"task.{_ACTIVITY[action]}", {"by": "member"}, actor_email=member_email, ref=task))
    return task_for_device(device, task.pk)  # type: ignore[return-value]
