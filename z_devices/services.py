"""
Business logic for z_devices. Views stay thin and call into here.

Side effects (analytics) are scheduled with `transaction.on_commit`, as in z_auth.
"""
from __future__ import annotations

import dataclasses
import hashlib
import re
from collections.abc import Iterable
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, F, OuterRef, Q, QuerySet, Subquery, Sum, Value
from django.db.models.functions import Coalesce, Left
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from z_analytics.services import track
from z_auth import entitlements as plan
from z_auth.entitlements import Entitlements, entitlements_for
from z_auth.models import User
from z_notification.services import notify

from . import blocklists, web_push
from .events import USB_LOCKED_DOWN, USB_RESTORED, build_title, is_confirmation
from .models import (
    AgentRelease,
    Device,
    DeviceEvent,
    Member,
    Message,
    Policy,
    UsageMetric,
    WorkLog,
    generate_member_code,
    hash_device_token,
)
from .work_time import day_so_far, day_start, local_day, work_zone

# How many of a member's recent messages the agent is handed per poll.
MESSAGE_FEED_LIMIT = 50
# The Chat tiles carry one short line of the latest message, never the message itself.
TILE_TEXT_MAX = 80


class EnrollmentRejected(Exception):
    """
    The agent's credentials don't let it join. `field` is the response key the
    agent keys its reaction off (see the contract): "member_id"/"email" mean a
    bad credential, "non_field_errors" means the member/owner is switched off.
    """

    def __init__(self, field: str, message: str, pc_limit: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.field = field
        self.message = message
        # Set when a PC was refused because the plan is full: the view tells the owner (after the rollback).
        self.pc_limit = pc_limit


class MemberAlreadyExists(Exception):
    """This owner already registered a member with this email."""


# Enrollment keys (the org-wide key) are retired (FIX-005): a PC enrols with its member's id + email
# (`enroll_device`). The EnrollmentKey table stays for history; nothing creates keys any more.


# --- Members ----------------------------------------------------------------


def _normalise_email(email: str) -> str:
    return email.strip().lower()


def generate_unique_member_code() -> str:
    """A ZM-XXXX-XXXX code not already taken (collisions are astronomically rare)."""
    for _ in range(10):
        code = generate_member_code()
        if not Member.objects.filter(member_code=code).exists():
            return code
    raise RuntimeError("Could not generate a unique member code.")  # pragma: no cover


@transaction.atomic
def create_member(owner: User, name: str, email: str, role: str = "") -> Member:
    """
    Register an employee under `owner` and mint their enrol id.

    Raises MemberAlreadyExists if this owner already has a member with this email.
    """
    email = _normalise_email(email)
    if owner.members.filter(email=email).exists():
        raise MemberAlreadyExists("A member with this email already exists.")
    try:
        member = Member.objects.create(
            owner=owner, name=name.strip(), email=email, role=role, member_code=generate_unique_member_code()
        )
    except IntegrityError as exc:  # the unique (owner, email) lost a race
        raise MemberAlreadyExists("A member with this email already exists.") from exc

    transaction.on_commit(lambda: _after_member_created(owner, member))
    return member


def _after_member_created(owner: User, member: Member) -> None:
    """Post-commit: track the event and email the member their id + the download link. Never raises."""
    track("member.created", {"email": member.email}, actor_email=owner.email, ref=member)
    send_member_invite(owner, member)


def send_member_invite(owner: User, member: Member, new_id: bool = False) -> None:
    """Email the member their Member ID + the download link (`new_id`: after "Get a new ID"). Never raises."""
    parts = member.name.split()
    company = (owner.company or "").strip() or _company_name(owner)
    notify(
        "email",
        member.email,
        "member_invite",
        {
            "first_name": parts[0] if parts else "there",
            "member_name": member.name,
            "member_email": member.email,
            "member_id": member.member_code,
            "company": company,
            "download_url": settings.ZINWE_AGENT_DOWNLOAD_URL,
            "new_id": new_id,
        },
        ref=member,
        redact=[member.member_code],  # the id is the enrol secret: masked in our logs, sent to the member
    )


@transaction.atomic
def rotate_member_code(member: Member, email: bool = False) -> Member:
    """
    Issue a fresh id for this member; the old one stops working immediately. `email`: re-send the invite
    with the new ID (J5-05), so their inbox never holds a dead ID.
    """
    member.member_code = generate_unique_member_code()
    member.save(update_fields=["member_code"])
    owner = member.owner
    transaction.on_commit(
        lambda: track(
            "member.id_rotated", {"email": member.email, "emailed": email}, actor_email=owner.email, ref=member
        )
    )
    if email and member.is_active:
        transaction.on_commit(lambda: send_member_invite(owner, member, new_id=True))
    return member


@transaction.atomic
def deactivate_member(member: Member) -> Member:
    """Switch a member off and revoke their PCs, so each one must stop and re-enrol."""
    member.is_active = False
    member.save(update_fields=["is_active"])
    revoked = member.devices.filter(is_revoked=False).update(is_revoked=True)
    transaction.on_commit(
        lambda: track(
            "member.deactivated",
            {"email": member.email, "devices_revoked": revoked},
            actor_email=member.owner.email,
            ref=member,
        )
    )
    return member


def members_for_owner(owner: User) -> QuerySet:
    """The owner's members, newest first, each with its devices prefetched."""
    return owner.members.prefetch_related("devices")


# --- Messages ---------------------------------------------------------------


def _company_name(owner: User) -> str:
    company = getattr(owner, "owned_company", None)
    return (getattr(company, "name", "") or owner.company or "").strip() or "your company"


def _first_name(name: str) -> str:
    parts = name.split()
    return parts[0] if parts else "there"


def send_message(
    owner: User,
    member: Member,
    title: str,
    body: str,
    severity: str,
    channel: str = Message.Channel.CHAT,
    event: DeviceEvent | None = None,
) -> Message:
    """
    Send `member` a short note: on their widget (`chat`), on the widget and by email
    (`chat_email`, so it reaches them even when their PC is off), or by email only
    (`email`: it stays in the owner's thread but never goes to the PC).

    `member` must belong to `owner` (the view resolves it from the owner's own
    members, so this is a defensive re-check). On commit, track the send and email it.
    """
    if member.owner_id != owner.id:
        raise ValueError("Member does not belong to this owner.")

    emailed = channel in (Message.Channel.CHAT_EMAIL, Message.Channel.EMAIL)
    message = Message.objects.create(
        owner=owner, member=member, sender=Message.Sender.OWNER, channel=channel,
        title=title, body=body, severity=severity,
        read_by_owner=True,  # the admin wrote it
    )
    transaction.on_commit(
        lambda: track(
            "member.message_sent",
            {"member_id": member.id, "severity": severity, "channel": channel, **({"emailed": True} if emailed else {})},
            actor_email=owner.email,
            ref=message,
        )
    )
    if emailed:
        transaction.on_commit(lambda: _email_message(owner, member, message))
    if event is not None:
        mark_event_handled(owner, event, True)  # messaging the person about an alert deals with it (J5-04)
    return message


def _handler_name(owner: User) -> str:
    return (owner.full_name or "").strip() or owner.email


def mark_event_handled(owner: User, event: DeviceEvent, handled: bool = True) -> DeviceEvent:
    """The owner dealt with this alert (or un-marks it). Already handled stays as first handled."""
    if handled and event.handled_at is None:
        event.handled_at = timezone.now()
        event.handled_by = _handler_name(owner)[:150]
    elif not handled:
        event.handled_at = None
        event.handled_by = ""
    else:
        return event
    event.save(update_fields=["handled_at", "handled_by"])
    return event


MESSAGE_EMAIL_TEMPLATE = "member_message"
ALERT_EMAIL_TEMPLATE = "member_alert"


def _email_message(owner: User, member: Member, message: Message) -> None:
    notify(
        "email",
        member.email,
        MESSAGE_EMAIL_TEMPLATE,
        {
            "first_name": _first_name(member.name),
            "company": _company_name(owner),
            "message": message,
            "urgent": message.severity == Message.Severity.CRITICAL,
            "in_widget": message.channel != Message.Channel.EMAIL,
        },
        ref=message,
    )


class NoMemberForEvent(Exception):
    """The PC behind this event isn't linked to a team member (nobody to email)."""


class AlreadyEmailed(Exception):
    def __init__(self, sent_at: Any) -> None:
        super().__init__("This alert was already emailed.")
        self.sent_at = sent_at


class EmailNotSent(Exception):
    """The email provider refused or failed; nothing was delivered."""


def alert_emails_sent(events: list[DeviceEvent]) -> dict[int, Any]:
    """event id -> when it was emailed to the member (only the ones that were)."""
    from z_notification.models import Notification

    if not events:
        return {}
    rows = Notification.objects.filter(
        template=ALERT_EMAIL_TEMPLATE,
        ref_type=DeviceEvent._meta.label_lower,
        ref_id__in=[str(e.pk) for e in events],
        status=Notification.Status.SENT,
    ).values_list("ref_id", "sent_at")
    return {int(ref_id): sent_at for ref_id, sent_at in rows}


def email_member_about_event(owner: User, event: DeviceEvent, meta: dict[str, str]) -> tuple[str, Any]:
    """
    Email the member whose PC raised `event` about it, once per event. `meta` is the
    dashboard's wording ({label, suggested}). Returns (recipient, sent_at).
    Raises NoMemberForEvent, AlreadyEmailed or EmailNotSent.
    """
    from z_notification.models import Notification

    member = event.device.member if event.device.member_id else None
    if member is None or not member.is_active:
        raise NoMemberForEvent()
    already = alert_emails_sent([event]).get(event.pk)
    if already is not None:
        raise AlreadyEmailed(already)
    sent = notify(
        "email",
        member.email,
        ALERT_EMAIL_TEMPLATE,
        {
            "first_name": _first_name(member.name),
            "company": _company_name(owner),
            "owner_name": owner.full_name or _company_name(owner),
            "label": meta.get("label") or build_title(event.type, event.data, event.device.hostname),
            "suggested": meta.get("suggested", ""),
            "what": build_title(event.type, event.data, event.device.hostname),
            "hostname": event.device.hostname,
            "occurred_at": event.occurred_at,
        },
        ref=event,
    )
    if sent is None or sent.status != Notification.Status.SENT:
        raise EmailNotSent()
    track("member.alert_emailed", {"type": event.type}, actor_email=owner.email, ref=event)
    return member.email, sent.sent_at


def reply_from_member(device: Device, body: str) -> Message | None:
    """
    A member replies from their widget (Device auth). One line in the thread,
    sender=member. Returns None when the PC isn't linked to a member.
    """
    if device.member_id is None:
        return None
    member = device.member
    message = Message.objects.create(
        owner_id=member.owner_id, member=member, sender=Message.Sender.MEMBER,
        title="", body=body, severity=Message.Severity.INFO,
        read_by_member=True,  # the member wrote it
    )
    transaction.on_commit(
        lambda: track("member.message_reply", {"member_id": member.id}, actor_email=member.email, ref=message)
    )
    web_push.announce(member.owner_id, "chat", [message.pk])
    return message


def messages_for_device(device: Device) -> QuerySet:
    """
    The device's member's conversation (both directions), newest first (empty if
    unlinked). Returns a QuerySet so an unlinked PC gets a cheap empty result.
    """
    if device.member_id is None:
        return Message.objects.none()
    return _with_tasks(
        Message.objects.filter(member_id=device.member_id).exclude(channel=Message.Channel.EMAIL)
    ).order_by("-created_at", "-id")[:MESSAGE_FEED_LIMIT]


def mark_delivered(device: Device) -> int:
    """
    Stamp the admin's messages as delivered: the member's PC has pulled them (called on the
    agent's poll). Only undelivered rows are touched, so a poll with nothing new writes nothing.
    """
    if device.member_id is None:
        return 0
    return Message.objects.filter(
        member_id=device.member_id, sender=Message.Sender.OWNER, delivered_at__isnull=True
    ).exclude(channel=Message.Channel.EMAIL).update(delivered_at=timezone.now())


def mark_read_by_member(device: Device) -> int:
    """
    Mark the admin's messages to this member as read (the agent calls this once the member
    opened them in the widget's chat). Returns how many. A read message is also delivered.
    """
    if device.member_id is None:
        return 0
    now = timezone.now()
    return Message.objects.filter(
        member_id=device.member_id, sender=Message.Sender.OWNER, read_by_member=False
    ).exclude(channel=Message.Channel.EMAIL).update(
        read_by_member=True,
        read_by_member_at=now,
        delivered_at=Coalesce(F("delivered_at"), Value(now)),
    )


def thread_for_owner(owner: User, member: Member) -> QuerySet:
    """The full conversation with one member (both directions), newest first. Owner-scoped."""
    return _with_tasks(Message.objects.filter(owner=owner, member=member)).order_by("-created_at", "-id")[
        :MESSAGE_FEED_LIMIT
    ]


def _with_tasks(qs: QuerySet) -> QuerySet:
    """FR-20: task lines carry their task (steps + activity) without a query per line."""
    return qs.select_related("task__member").prefetch_related("task__steps", "task__activity")


def mark_thread_read_by_owner(member: Member) -> int:
    """Mark the member's replies as seen by the admin (called when the admin opens the chat)."""
    return Message.objects.filter(
        member=member, sender=Message.Sender.MEMBER, read_by_owner=False
    ).update(read_by_owner=True)


def _tile_text(kind: str, title: str, body: str) -> str:
    """One short line for a Chat tile: the first line of what was said, or "Task: <title>" for a task line."""
    if kind == Message.Kind.TASK:
        text = f"Task: {title}"
    else:
        text = next((line.strip() for line in body.splitlines() if line.strip()), title)
    return text if len(text) <= TILE_TEXT_MAX else f"{text[:TILE_TEXT_MAX - 1].rstrip()}…"


def chat_tiles(owner: User, members: Iterable[Member]) -> dict[int, dict[str, Any]]:
    """
    What the Chat page's tiles need, by member id, in a fixed number of queries however many members there are:
    {"unread": int, "last_message": {"text", "sender", "at"} | None}. The newest line uses the
    Message(member, -created_at) index.

    The text is the conversation's own, so it follows the thread's rule: without the plan's Chat module
    (`plan.require` in MemberThreadView) `last_message` stays None. Reading tiles marks nothing read.
    """
    ids = [member.pk for member in members]
    unread = dict(
        Message.objects.filter(member_id__in=ids, sender=Message.Sender.MEMBER, read_by_owner=False)
        .order_by()
        .values_list("member_id")
        .annotate(count=Count("id"))
    )
    last: dict[int, dict[str, Any]] = {}
    if entitlements_for(owner).has(plan.TEAM_CHAT):
        newest = Message.objects.filter(member=OuterRef("pk")).order_by("-created_at", "-id").values("pk")[:1]
        rows = Message.objects.filter(
            pk__in=Member.objects.filter(pk__in=ids, owner=owner).annotate(newest=Subquery(newest)).values("newest")
        ).order_by().values("member_id", "kind", "sender", "title", "created_at", start=Left("body", 2 * TILE_TEXT_MAX))
        for row in rows:
            last[row["member_id"]] = {
                "text": _tile_text(row["kind"], row["title"], row["start"]),
                "sender": row["sender"],
                "at": row["created_at"],
            }
    return {pk: {"unread": unread.get(pk, 0), "last_message": last.get(pk)} for pk in ids}


def current_device_for(member: Member) -> Device | None:
    """
    The member's most recently seen PC (prefetched devices sorted in Python). A PC their day moved away from
    comes last: it keeps heartbeating, but it isn't where the day is.
    """
    devices = list(member.devices.all())
    if not devices:
        return None
    return max(
        devices,
        key=lambda d: (d.work_moved_to_id is None, d.last_seen_at or d.enrolled_at, d.id),
    )


# --- Agent lifecycle --------------------------------------------------------


@transaction.atomic
def enroll_device(data: dict[str, Any], ip: str | None) -> tuple[Device, str, bool]:
    """
    Join a PC to the member identified by `data["member_id"]` + `data["email"]`.

    The member code is the enrol secret; the email must match the one the owner
    registered. Returns (device, raw_token, created). Re-enrolling the same
    machine (agent reinstall) rotates its token and reactivates it rather than
    creating a duplicate row. The raw token is returned exactly once.

    Raises EnrollmentRejected with the response field the agent keys off:
      member_id       -- no such (active) code
      email           -- code is real but the email doesn't match
      non_field_errors-- member or owner switched off
    """
    member = (
        Member.objects.select_for_update()
        .select_related("owner")
        .filter(member_code=data["member_id"])
        .first()
    )
    if member is None:
        raise EnrollmentRejected("member_id", "This member id is not valid. Ask your admin.")
    if _normalise_email(data["email"]) != member.email:
        raise EnrollmentRejected("email", "This email does not match the member id.")
    if not member.is_active or not member.owner.is_active:
        raise EnrollmentRejected("non_field_errors", "This member is no longer active.")
    _check_pc_limit(member.owner, data["machine_id"])

    now = timezone.now()
    token = Device.new_token()
    # A hand-over belongs to the member it happened to: this PC may be joining as someone else. (Reinstalled for the
    # same member it keeps its place: Start day there is still a move back.)
    Device.objects.filter(owner=member.owner, machine_id=data["machine_id"]).exclude(member=member).update(
        work_moved_to=None, work_moved_at=None, work_carry=None
    )
    device, created = Device.objects.update_or_create(
        owner=member.owner,
        machine_id=data["machine_id"],
        defaults={
            "member": member,
            "hostname": data["hostname"],
            "platform": data["platform"],
            "os_version": data.get("os_version", ""),
            "agent_version": data.get("agent_version", ""),
            "token_hash": hash_device_token(token),
            "is_revoked": False,
            "last_seen_at": now,
            "last_ip": ip,
        },
    )

    transaction.on_commit(
        lambda: track(
            "device.enrolled",
            {
                "hostname": device.hostname,
                "platform": device.platform,
                "re_enrolled": not created,
                "member_email": member.email,
            },
            actor_email=member.owner.email,
            ref=device,
        )
    )
    return device, token, created


def _check_pc_limit(owner: User, machine_id: str) -> None:
    """
    Plan enforcement: a NEW PC (or a removed one coming back) needs a free slot on the owner's plan.
    A PC already enrolled can always re-enrol (agent reinstall), so going over the limit after a
    downgrade never cuts off PCs that are working -- only adding more is refused.
    """
    existing = Device.objects.filter(owner=owner, machine_id=machine_id).only("is_revoked").first()
    if existing is not None and not existing.is_revoked:
        return
    ent = entitlements_for(owner)
    if not ent.max_devices or plan.devices_used(owner) < ent.max_devices:
        return
    track(
        "device.enroll_refused",
        {"reason": "pc_limit", "max_devices": ent.max_devices, "plan": ent.plan.code if ent.plan else ""},
        actor_email=owner.email,
    )
    raise EnrollmentRejected(
        "non_field_errors",
        f"Your company's plan covers {ent.max_devices} PCs and they're all in use. "
        "Ask your admin to upgrade the plan or remove a PC.",
        pc_limit={"owner_id": owner.pk, "max_devices": ent.max_devices, "plan": ent.plan.name if ent.plan else ""},
    )


PC_LIMIT_TEMPLATE = "pc_limit_reached"
PC_LIMIT_EMAIL_EVERY = timedelta(hours=6)


def tell_owner_pc_refused(exc: EnrollmentRejected, member_email: str, hostname: str) -> None:
    """
    A PC was refused because the plan is full (J3-19): email the owner, at most once every few hours.
    Called by the view after the enrolment transaction rolled back. Never raises.
    """
    from z_notification.models import Notification

    info = exc.pc_limit or {}
    owner = User.objects.filter(pk=info.get("owner_id")).first()
    if owner is None:
        return
    recent = Notification.objects.filter(
        template=PC_LIMIT_TEMPLATE, recipient=owner.email, created_at__gte=timezone.now() - PC_LIMIT_EMAIL_EVERY,
    ).exists()
    if recent:
        return
    member = owner.members.filter(email=_normalise_email(member_email)).first()
    app = settings.ZINWE_APP_URL
    # Name the smallest plan that fits their team (from sign-up) and one more PC, and link straight to it.
    from z_auth.announcements import team_size_upper
    from z_auth.models import Plan

    max_devices = int(info.get("max_devices") or 0)
    need = max(max_devices + 1, team_size_upper(owner))
    fits = (
        Plan.objects.filter(is_active=True)
        .filter(Q(max_devices=0) | Q(max_devices__gte=need))
        .order_by("price_inr_monthly", "sort_order")
        .first()
    )
    notify(
        "email",
        owner.email,
        PC_LIMIT_TEMPLATE,
        {
            "first_name": _first_name(owner.full_name or ""),
            "member_name": member.name if member else member_email,
            "hostname": hostname,
            "max_devices": info.get("max_devices"),
            "plan_name": info.get("plan", ""),
            "upgrade_url": f"{app}/onboarding/plan" + (f"?plan={fits.code}" if fits else ""),
            "suggested_plan": fits.name if fits else "",
            "suggested_pcs": fits.max_devices if fits else 0,
            "team_url": f"{app}/app/team",
        },
        ref=owner,
    )


def _record_consent(device: Device, data: dict[str, Any], now: Any) -> None:
    """
    Keep the member's answer on the widget's consent screen. Newer agents send
    `consent: {state: given|declined, version}`; older ones `consent_version` + `consent_at` (= given).
    Declining is allowed (owner decision 2026-10-17): nothing is collected from that PC afterwards.
    """
    consent = data.get("consent") or None
    if consent:
        version = (consent.get("version") or "").strip()[:20]
        if consent["state"] == "declined":
            if device.consent_state != Device.CONSENT_DECLINED:
                device.consent_declined_at = now
                device.consent_at = None
                device.consent_version = version
            return
        if device.consent_state == Device.CONSENT_GIVEN and (not version or version == device.consent_version):
            return
        device.consent_version = version or device.consent_version
        device.consent_at = now
        device.consent_declined_at = None
        return
    version = (data.get("consent_version") or "").strip()[:20]
    if not version or version == device.consent_version:
        return
    at = data.get("consent_at") or now
    if at > now:
        at = now
    device.consent_version = version
    device.consent_at = at
    device.consent_declined_at = None


# What a PC whose member declined consent still does: work hours and chat. Nothing else is collected.
DECLINED_MODULES = frozenset({plan.WORK_TIME, plan.TEAM_CHAT})


def device_entitlements(device: Device, ent: Entitlements | None = None) -> Entitlements:
    """
    The owner's plan as it applies to this PC: when its member declined monitoring, only work hours and
    chat stay on, so the policy it's sent switches every collection and block off (and its version changes,
    so the agent re-fetches), and its monitoring uploads are dropped.
    """
    ent = ent or entitlements_for(device.owner)
    if device.consent_state != Device.CONSENT_DECLINED:
        return ent
    return dataclasses.replace(ent, modules=frozenset(ent.modules & DECLINED_MODULES))


def monitoring_allowed(device: Device) -> bool:
    return device.consent_state != Device.CONSENT_DECLINED


LOGGED_WORK_STATES = {Device.WorkState.WORKING, Device.WorkState.ON_BREAK, Device.WorkState.ENDED}
# A day in progress: the states that make a PC the one that owns the member's day.
ACTIVE_WORK_STATES = (Device.WorkState.WORKING, Device.WorkState.ON_BREAK)
# What the owner approves or questions: breaks, and since FR-16 log-outs too.
APPROVED_STATES = {Device.WorkState.ON_BREAK, Device.WorkState.ENDED}


def _log_work_change(device: Device, data: dict[str, Any], now: Any) -> WorkLog | None:
    """
    Add a WorkLog row when the reported work state differs from the stored one
    (started, break, back, ended). The widget's own timestamp is trusted when it's
    plausible (not in the future, within the last day); otherwise we use `now`.
    """
    new_state = data.get("work_state")
    if new_state == device.work_state or new_state not in LOGGED_WORK_STATES:
        return None
    at = data.get("work_changed_at") or now
    if at > now or at < now - timedelta(days=1):
        at = now
    on_break = new_state == Device.WorkState.ON_BREAK
    # A break carries the break reason/note; logging out carries its own (the widget asks for both).
    prefix = "break" if on_break else "end" if new_state == Device.WorkState.ENDED else None
    # Breaks and log-outs wait for the owner's OK, after the fact (FR-16: log-outs like breaks).
    needs_ok = new_state in APPROVED_STATES
    reason = (data.get(f"{prefix}_reason") or "").strip()[:120] if prefix else ""
    note = (data.get(f"{prefix}_note") or "").strip()[:280] if prefix else ""
    log = WorkLog.objects.create(
        owner_id=device.owner_id, device=device, member_id=device.member_id,
        state=new_state, previous_state=device.work_state or "", at=at, reason=reason, note=note,
        approval=WorkLog.Approval.PENDING if needs_ok else "",
    )
    if needs_ok:
        web_push.announce(device.owner_id, "approval", [log.pk])
    return log


WORK_EVENT_MAX_AGE = timedelta(days=7)
WORK_EVENT_MAX_SKEW = timedelta(minutes=5)
WORK_EVENT_SAME_WITHIN = timedelta(seconds=2)


def _log_work_events(device: Device, events: list[dict[str, Any]], now: Any) -> int:
    """
    Record each change the widget logged (log in, break, back, log out) at its exact time, once.

    Newer agents send every change since the last heartbeat they got through, so a 20-second
    break or changes made while offline all reach Activity. Re-sent events (a lost response, a
    retry) are skipped by their timestamp; so are ones far in the past or the future. The
    `previous_state` is whatever this PC's log said just before, which tells "logged in" from
    "back from break". Returns how many rows were added.
    """
    added = 0
    tz = work_zone(device.owner)
    waiting: list[int] = []  # rows that wait for the owner's OK
    for event in sorted(events, key=lambda e: e["at"]):
        at, state = event["at"], event["state"]
        if at > now + WORK_EVENT_MAX_SKEW or at < now - WORK_EVENT_MAX_AGE:
            continue
        # Same change already recorded (a retry, or the older state-diff path saw it): same state within
        # a couple of seconds of the same moment.
        if WorkLog.objects.filter(
            device=device, state=state, at__range=(at - WORK_EVENT_SAME_WITHIN, at + WORK_EVENT_SAME_WITHIN)
        ).exists():
            continue
        last = (
            WorkLog.objects.filter(device=device, at__lt=at)
            .order_by("-at", "-id")
            .values_list("state", "at")
            .first()
        )
        previous = last[0] if last else ""
        # Yesterday's forgotten log-out doesn't swallow today's log in: the day counts from that line.
        if previous == state and local_day(last[1], tz) == local_day(at, tz):
            continue
        explained = state in APPROVED_STATES
        log = WorkLog.objects.create(
            owner_id=device.owner_id, device=device, member_id=device.member_id,
            state=state, previous_state=previous, at=at,
            reason=(event.get("reason") or "").strip()[:120] if explained else "",
            note=(event.get("note") or "").strip()[:280] if explained else "",
            approval=WorkLog.Approval.PENDING if explained else "",
        )
        added += 1
        if explained:
            waiting.append(log.pk)
    web_push.announce(device.owner_id, "approval", waiting)
    return added


# --- One person, one work day, across PCs ------------------------------------

# The Device fields a hand-over changes.
HANDOVER_FIELDS = [
    "work_state", "worked_seconds", "break_seconds", "work_since", "work_moved_to", "work_moved_at", "work_carry",
]


def _start_after(data: dict[str, Any], moved_at: Any, now: Any) -> Any:
    """
    When a PC the day moved away from was started again, or None: the first "working" change after the move. An
    agent that hasn't heard of the move yet (or never will: an older one) keeps reporting the day it began before
    the move, which is not a new start. Any press after the move means the member is back at this PC.
    """
    events = data.get("work_events")
    if events is not None:
        return min(
            (e["at"] for e in events if e["state"] == Device.WorkState.WORKING
             and moved_at < e["at"] <= now + WORK_EVENT_MAX_SKEW),
            default=None,
        )
    changed = data.get("work_changed_at")
    return changed if data["work_state"] == Device.WorkState.WORKING and changed and moved_at < changed <= now else None


def _last_start(device: Device, midnight: Any) -> Any:
    """When this PC last started (or took over) today's day. Coming back from a break isn't a start."""
    return (
        WorkLog.objects.filter(device=device, state=WorkLog.State.WORKING, at__gte=midnight)
        .exclude(previous_state=WorkLog.State.ON_BREAK, moved_from="")
        .order_by("-at", "-id")
        .values_list("at", flat=True)
        .first()
    )


def _start_line(device: Device, at: Any) -> WorkLog:
    """The "working" line of the start at `at`: the one just logged, or a new one (a PC restarting may have none)."""
    near = (at - WORK_EVENT_SAME_WITHIN, at + WORK_EVENT_SAME_WITHIN)
    return WorkLog.objects.filter(device=device, state=WorkLog.State.WORKING, at__range=near).first() or (
        WorkLog.objects.create(
            owner_id=device.owner_id, device=device, member_id=device.member_id, state=WorkLog.State.WORKING, at=at,
            previous_state=WorkLog.objects.filter(member_id=device.member_id, at__lt=at)
            .order_by("-at", "-id").values_list("state", flat=True).first() or "",
        )
    )


def _carry(device: Device, line: WorkLog, tz: Any, from_hostname: str = "") -> dict[str, Any]:
    """What the member's day adds up to when `line` starts, wherever it ran, for the timer that carries it on."""
    so_far = day_so_far(WorkLog.objects.filter(member_id=device.member_id), line.at, tz)
    return {
        "at": line.at.isoformat(),
        "worked_seconds": so_far.worked,
        "break_seconds": so_far.on_break,
        "breaks": so_far.breaks,
        "first_start": so_far.first_in.isoformat() if so_far.first_in else None,
        "from_hostname": from_hostname,
    }


def _hand_over(loser: Device, winner: Device, at: Any, tz: Any) -> None:
    """
    The member's day moves from `loser` to `winner` at `at`: one "Moved to" line on the winner, no log-out, nothing
    to approve. The loser's work state stops counting from here; it stays enrolled and keeps heartbeating.
    A PC that had gone quiet earlier (switched off, offline) left off at its last heartbeat, not at the move.
    """
    quiet_since = at - timedelta(seconds=settings.ZINWE_DEVICE_OFFLINE_AFTER_SECONDS)
    line = _start_line(winner, at)
    line.moved_from = loser.hostname
    line.left_off_at = loser.last_seen_at if loser.last_seen_at and loser.last_seen_at < quiet_since else None
    line.previous_state = loser.work_state
    line.save(update_fields=["moved_from", "left_off_at", "previous_state"])

    winner.work_moved_to, winner.work_moved_at = None, None
    winner.work_carry = _carry(winner, line, tz, loser.hostname)
    loser.work_moved_to, loser.work_moved_at, loser.work_carry = winner, at, None
    loser.work_state = Device.WorkState.NOT_STARTED
    loser.worked_seconds, loser.break_seconds, loser.work_since = 0, 0, None
    winner.save(update_fields=HANDOVER_FIELDS)
    loser.save(update_fields=HANDOVER_FIELDS)
    # PCs the day left earlier now point at where it is.
    Device.objects.filter(work_moved_to=loser).exclude(pk=winner.pk).update(work_moved_to=winner)


def _settle_work_day(device: Device, data: dict[str, Any], now: Any) -> bool:
    """
    Log this heartbeat's work changes and decide which of the member's PCs owns the day: last Start day wins. Runs
    inside the heartbeat's transaction. The member's row is the lock, so two of their PCs heartbeating at once take
    turns, and what the other one changed is read fresh here. Returns False when this PC doesn't own the day: what
    it reports isn't stored.
    """
    tz = work_zone(device.owner)
    today = local_day(now, tz)
    if device.member_id:
        Member.objects.select_for_update().filter(pk=device.member_id).first()
        device.refresh_from_db(fields=HANDOVER_FIELDS)
    # Midnight: a move or a carry is good for its own day only.
    if device.work_moved_at and (device.work_moved_to_id is None or local_day(device.work_moved_at, tz) != today):
        device.work_moved_to, device.work_moved_at = None, None
    if device.work_carry and local_day(parse_datetime(device.work_carry["at"]), tz) != today:
        device.work_carry = None

    events = data.get("work_events")
    start = None
    if device.work_moved_to_id:
        moved_at = device.work_moved_at
        start = _start_after(data, moved_at, now)
        if start is None:
            # What it did before the move (it was offline) still counts; nothing after it does.
            if events is not None:
                _log_work_events(device, [e for e in events if e["at"] <= moved_at], now)
            return False
        if events is not None:
            events = [e for e in events if e["at"] <= moved_at or e["at"] >= start]
        device.work_moved_to, device.work_moved_at = None, None
    if events is not None:
        _log_work_events(device, events, now)
    else:
        _log_work_change(device, data, now)

    if not device.member_id or (start is None and data["work_state"] not in ACTIVE_WORK_STATES):
        return True
    midnight = day_start(today, tz)
    rivals = list(
        Device.objects.filter(
            member_id=device.member_id, is_revoked=False, work_moved_to__isnull=True,
            work_state__in=ACTIVE_WORK_STATES, work_since__gte=midnight,
        ).exclude(pk=device.pk)
    )
    if not rivals:
        if start is not None:  # back on this PC after the day ended elsewhere: no move, but the timer still carries on
            device.work_carry = _carry(device, _start_line(device, start), tz)
        return True
    mine = start or _last_start(device, midnight) or min(data.get("work_changed_at") or now, now)
    contenders = [(mine, device), *((_last_start(r, midnight) or r.work_since, r) for r in rivals)]
    # The latest start wins; a tie goes to the PC reporting now.
    at, winner = max(contenders, key=lambda c: (c[0], c[1] is device))
    for _, pc in contenders:
        if pc is not winner:
            _hand_over(pc, winner, at, tz)
    return winner is device


class NotABreak(Exception):
    """Only breaks and log-outs can be approved or denied."""


def decide_break(owner: User, log: WorkLog, decision: str, note: str = "") -> WorkLog:
    """
    Approve or deny a break or a log-out (FR-16) after the fact: the member isn't blocked. The member is told
    in their Zinwe widget, with the owner's note if any. Deciding again overwrites.
    """
    if log.owner_id != owner.id:
        raise PermissionError("Not your team's break.")
    if log.state not in APPROVED_STATES:
        raise NotABreak()
    log.approval = decision
    log.decision_note = note.strip()[:280]
    log.decided_at = timezone.now()
    log.save(update_fields=["approval", "decision_note", "decided_at"])
    if log.member_id and log.member and log.member.is_active:
        is_break = log.state == WorkLog.State.ON_BREAK
        what = (f"{log.reason} break" if log.reason else "break") if is_break else "log-out"
        when = timezone.localtime(log.at).strftime("%I:%M %p").lstrip("0")
        approved = decision == WorkLog.Approval.APPROVED
        # Kind wording either way (J6-18): a questioned break is a conversation, not a warning. Without a note
        # the member is invited to reply, so they're never left guessing.
        manager = owner.full_name.split()[0] if owner.full_name.strip() else "Your manager"
        body = log.decision_note or (
            "" if approved else f"{manager} would like a word about this {'break' if is_break else 'log-out'}. You can reply here."
        )
        send_message(
            owner, log.member,
            title=(f"Your {what} at {when} was approved" if approved else f"About your {what} at {when}")[:120],
            body=body,
            severity=Message.Severity.INFO,
        )
    return log


def work_log_for_owner(owner: User, day: Any, member_id: int | None = None) -> QuerySet:
    """The owner's work-day changes on one local calendar day, oldest first (none before the plan's history)."""
    from .work_time import day_bounds, work_zone

    tz = work_zone(owner)
    since = entitlements_for(owner).history_since()
    if since is not None and day < timezone.localtime(since, tz).date():
        return WorkLog.objects.none()
    lo, hi = day_bounds(day, day, tz)
    logs = WorkLog.objects.filter(owner=owner, at__gte=lo, at__lt=hi).select_related("device", "member")
    if member_id is not None:
        logs = logs.filter(member_id=member_id)
    return logs.order_by("at", "id")


def work_day_totals(owner: User, day: Any, member_id: int | None = None, now: Any = None) -> dict[str, Any]:
    """
    FIX-011: each person's time on one day for Activity, split at midnight like reports (work_time.py).
    `carried_in` are the changes from the day before whose stretch was still running at midnight (log in 22:00,
    log out 02:00: the 22:00 log-in is carried into the second day); `totals` has one row per person.
    """
    from .work_time import day_bounds, work_days, work_zone

    tz = work_zone(owner)
    since = entitlements_for(owner).history_since()
    if since is not None and day < timezone.localtime(since, tz).date():
        return {"carried_in": [], "totals": []}
    lo, hi = day_bounds(day, day, tz)
    logs = WorkLog.objects.filter(owner=owner)
    if member_id is not None:
        logs = logs.filter(member_id=member_id)
    carried: list[WorkLog] = []
    totals = []
    for key, sample, days in work_days(logs, lo, hi, now or timezone.now(), tz, since=since):
        w = days.get(day)
        if w is None:
            continue
        if w.carried_in is not None:
            carried.append(w.carried_in)
        totals.append({
            "key": f"{key[0]}{key[1]}", "member_id": sample.member_id, "device_id": sample.device_id,
            "worked_seconds": w.worked, "break_seconds": w.on_break, "breaks": w.breaks, "logged_in": w.logged_in,
            "open_ended": w.open_ended, "continues": w.continues,
            "carried_in": w.carried_in.pk if w.carried_in is not None else None,
        })
    return {"carried_in": carried, "totals": totals}


def record_heartbeat(device: Device, data: dict[str, Any], ip: str | None) -> dict[str, Any]:
    """Mark the device as seen, store its work status, and tell the agent when to call back."""
    now = timezone.now()
    ent = entitlements_for(device.owner)
    device.last_seen_at = now
    device.last_ip = ip
    for field in ("agent_version", "os_version", "hostname"):
        if data.get(field):
            setattr(device, field, data[field])
    # Work status (M6). The serializer supplies sane defaults for an older agent
    # that omits these, so validated_data always carries them. A newer agent sends the
    # exact list of changes; an older one only its current state (we log what changed).
    # Plan enforcement: the work log (Activity) is only kept on plans with work hours.
    with transaction.atomic():  # the member's row locks their day (_settle_work_day): PostgreSQL needs a transaction
        owns_day = ent.has(plan.WORK_TIME) and _settle_work_day(device, data, now)
        _record_consent(device, data, now)
        if owns_day:
            if data["work_state"] != device.work_state or data.get("work_changed_at"):
                # When this state began (the widget's last change; older agents: when we first heard of it).
                since = data.get("work_changed_at") or now
                device.work_since = min(since, now)
            device.work_state = data["work_state"]
            # Until the timer has taken the carry-over (and for ever on an older agent) the day still adds up to it.
            carry = device.work_carry or {}
            device.worked_seconds = max(data["worked_seconds"], carry.get("worked_seconds", 0))
            device.break_seconds = max(data["break_seconds"], carry.get("break_seconds", 0))
        elif not ent.has(plan.WORK_TIME):
            # Plan enforcement: no work hours on this plan, so the PC's work day isn't kept either
            # (the dashboard shows it as plain online/offline).
            device.work_state = Device.WorkState.NOT_STARTED
            device.worked_seconds = 0
            device.break_seconds = 0
            device.work_since = None
            device.work_moved_to = None
            device.work_moved_at = None
            device.work_carry = None
        device.work_updated_at = now
        # "Restore USB" (J5-02): told once, on the first heartbeat after the owner pressed it.
        usb_restore = bool(
            device.usb_restore_requested_at
            and (device.usb_restore_delivered_at is None or device.usb_restore_delivered_at < device.usb_restore_requested_at)
        )
        if usb_restore:
            device.usb_restore_delivered_at = now
        device.save(
            update_fields=[
                "last_seen_at", "last_ip", "agent_version", "os_version", "hostname",
                "work_state", "worked_seconds", "break_seconds", "work_updated_at", "work_since",
                "work_moved_to", "work_moved_at", "work_carry",
                "consent_version", "consent_at", "consent_declined_at", "usb_restore_delivered_at",
            ]
        )
    policy = get_policy(device.owner)
    response = {
        "device_id": device.id,
        "server_time": device.last_seen_at,
        "next_heartbeat_seconds": settings.ZINWE_DEVICE_HEARTBEAT_SECONDS,
        # The agent re-fetches GET policy/current/ whenever this differs from its cache (a declined
        # consent changes it too: that PC's policy has monitoring off).
        "policy_version": effective_policy_version(policy, device_entitlements(device, ent)),
        # The set of sites this PC should hold: moves on its own when a new list version ships.
        "web_list_version": device_list_version(device, policy, ent),
        "consent_state": device.consent_state,
        "usb_restore": usb_restore,
        **employer_names(device),
    }
    # The day moved (keys omitted otherwise, like "update"). Sent on every heartbeat of that day, so a lost response
    # costs nothing: the old PC hears where the day went, the new PC what to carry on from (it takes each one once).
    if device.work_moved_to_id:
        response["day_moved"] = {"to_hostname": device.work_moved_to.hostname, "at": device.work_moved_at}
    elif device.work_carry:
        response["day_carry"] = device.work_carry
    # Updates (M12 + Releases): a newer published release is offered to the agent ("update"),
    # and a required one (or the env minimum) also pauses the member's work ("update_required").
    # Keys are omitted entirely when no update is due, so the response shape is unchanged.
    update = pending_update(device.agent_version)
    if update is not None:
        response["update"] = update
        if update["required"]:
            response["update_required"] = True
            response["download_url"] = settings.ZINWE_AGENT_DOWNLOAD_URL
    return response


VERSION_PARTS = 4


def _parse_version(value: str) -> tuple[int, ...]:
    """
    "0.3.0" -> (0, 3, 0, 0). Build metadata / pre-release tags are dropped ("0.3.0+abc12" is 0.3.0),
    and the tuple is padded so "0.3" == "0.3.0". "" or garbage -> () (older than everything).
    """
    core = (value or "").strip().split("+", 1)[0].split("-", 1)[0]
    parts: list[int] = []
    for chunk in core.split("."):
        chunk = chunk.strip()
        if not chunk.isdigit():
            break
        parts.append(int(chunk))
    if not parts:
        return ()
    return tuple(parts[:VERSION_PARTS]) + (0,) * (VERSION_PARTS - min(len(parts), VERSION_PARTS))


def is_valid_version(value: str) -> bool:
    """A release version the agent can compare: 2-4 dotted numbers, e.g. 0.3.0."""
    return bool(re.fullmatch(r"\d+(\.\d+){1,3}", (value or "").strip()))


def update_required(agent_version: str) -> bool:
    """True when `agent_version` is below the env minimum ZINWE_AGENT_MIN_VERSION ("" = never)."""
    minimum = _parse_version(settings.ZINWE_AGENT_MIN_VERSION)
    return bool(minimum) and _parse_version(agent_version) < minimum


def release_download_path(release: AgentRelease) -> str:
    """Where the agent downloads the MSI: our device-authenticated endpoint, or the external URL."""
    if release.file:
        return f"/api/devices/releases/{release.pk}/download/"
    return release.download_url


def pending_update(agent_version: str) -> dict[str, Any] | None:
    """
    What to offer an agent running `agent_version`: the newest active release above it, required
    if ANY newer release is required (skipping a required release must not dodge it) or the agent
    is below the env minimum. None when it's up to date.
    """
    current = _parse_version(agent_version)
    newer = [r for r in AgentRelease.objects.filter(is_active=True) if _parse_version(r.version) > current]
    below_floor = update_required(agent_version)
    if not newer:
        if not below_floor:
            return None
        # Only the env minimum applies (no release uploaded): manual download, as before.
        return {"version": settings.ZINWE_AGENT_MIN_VERSION, "notes": "", "required": True,
                "download_url": "", "sha256": "", "size": None}
    latest = max(newer, key=lambda r: _parse_version(r.version))
    return {
        "version": latest.version,
        "notes": latest.notes,
        "required": below_floor or any(r.required for r in newer),
        "download_url": release_download_path(latest),
        "sha256": latest.sha256,
        "size": latest.size,
    }


class ReleaseRefused(Exception):
    """The release can't be published as given (bad version, duplicate, no file/hash)."""


def publish_release(
    staff: User, version: str, notes: str, required: bool,
    upload: Any = None, download_url: str = "", sha256: str = "",
) -> AgentRelease:
    """
    Publish a new agent release. With an uploaded MSI we compute its SHA-256 and size ourselves;
    with an external `download_url` the operator supplies the SHA-256. PCs see it on their next
    heartbeat (within a minute).
    """
    version = (version or "").strip()
    if not is_valid_version(version):
        raise ReleaseRefused("Version must look like 0.3.0.")
    if AgentRelease.objects.filter(version=version).exists():
        raise ReleaseRefused(f"Version {version} is already published.")
    release = AgentRelease(version=version, notes=(notes or "").strip(), required=required, published_by=staff)
    if upload is not None:
        if not upload.name.lower().endswith(".msi"):
            raise ReleaseRefused("Upload the Zinwe Agent installer (.msi).")
        digest = hashlib.sha256()
        for chunk in upload.chunks():
            digest.update(chunk)
        upload.seek(0)
        release.sha256 = digest.hexdigest()
        release.size = upload.size
        release.file.save(f"ZinweAgent-{version}.msi", upload, save=False)
    else:
        sha256 = (sha256 or "").strip().lower()
        if not download_url or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ReleaseRefused("Upload the .msi, or give a download URL and its SHA-256.")
        release.download_url = download_url
        release.sha256 = sha256
    release.save()
    transaction.on_commit(lambda: track(
        "agent.release_published", {"version": version, "required": required}, actor_email=staff.email, ref=release
    ))
    return release


def release_adoption(release: AgentRelease) -> dict[str, int]:
    """How many active PCs already run this release (or newer), out of all active PCs."""
    target = _parse_version(release.version)
    versions = Device.objects.filter(is_revoked=False).values_list("agent_version", flat=True)
    total = updated = 0
    for v in versions:
        total += 1
        updated += _parse_version(v) >= target
    return {"updated": updated, "total": total}


# --- Policy -----------------------------------------------------------------


def get_policy(owner: User) -> Policy:
    """The owner's policy, created on first use (version 1, USB allowed)."""
    policy, _ = Policy.objects.get_or_create(owner=owner)
    return policy


# --- Plan enforcement: the policy the agent actually gets ----------------------
#
# The owner's saved rules stay as they are; what a PC is SENT is clamped to the plan, so an
# upgrade switches features on (and a downgrade off) without touching the saved rules. The
# version the agent compares folds in the plan's module set, so a plan change alone makes
# every PC re-fetch. Old agents ignore the extra `entitlements` / `max_devices` keys.

# A warn size no transfer reaches: the data-transfer module keeps watching USB copies
# (USB alerts are on every plan) but never raises a network-folder or Outlook alert.
UNREACHABLE_BYTES = 2**62


def effective_policy_version(policy: Policy, ent: Entitlements) -> int:
    return policy.version * 1000 + ent.fingerprint


def apply_entitlements(data: dict[str, Any], policy: Policy, ent: Entitlements) -> dict[str, Any]:
    """Clamp the agent's policy wire shape (DevicePolicySerializer data) to what `ent` includes."""
    out = {key: dict(value) if isinstance(value, dict) else value for key, value in data.items()}
    if not ent.has(plan.USB_CONTROL):
        out["usb"]["mode"] = Policy.UsbMode.ALLOW
        out["data_transfer"]["usb_lockdown"] = False
    if not ent.has(plan.USB_ALERTS) and not ent.has(plan.DATA_TRANSFER):
        out["data_transfer"]["enabled"] = False
    if not ent.has(plan.DATA_TRANSFER):
        out["data_transfer"]["lan_warn_bytes"] = UNREACHABLE_BYTES
        out["data_transfer"]["email_warn_bytes"] = UNREACHABLE_BYTES
        out["data_transfer"]["web_upload_warn_bytes"] = UNREACHABLE_BYTES
    if not ent.has(plan.REMOTE_ACCESS):
        out["remote_software"]["blocking_enabled"] = False
        if not ent.has(plan.SOFTWARE_INSTALLS):
            out["remote_software"]["enabled"] = False
    if not ent.has(plan.WEB):
        out["web"]["enabled"] = False
    out["version"] = effective_policy_version(policy, ent)
    out["entitlements"] = sorted(ent.modules)
    out["max_devices"] = ent.max_devices
    return out


def employer_names(device: Device) -> dict[str, str]:
    """Who the employee works for, so the agent's consent notice and messages can name them ("" when unknown)."""
    owner = device.owner
    company = getattr(owner, "owned_company", None)
    return {"company_name": company.name if company else "", "owner_name": owner.full_name or ""}


def policy_for_device(device: Device, data: dict[str, Any], policy: Policy) -> dict[str, Any]:
    """The rules this PC enforces: the owner's, clamped to the plan and to the member's consent."""
    out = apply_entitlements(data, policy, device_entitlements(device))
    declined = device.consent_state == Device.CONSENT_DECLINED
    out["consent_state"] = device.consent_state
    out["monitoring_enabled"] = not declined
    out.update(employer_names(device))
    if declined:
        out["usb"]["mode"] = Policy.UsbMode.ALLOW
        out["data_transfer"]["enabled"] = False
        out["data_transfer"]["usb_lockdown"] = False
        out["data_transfer"]["include_details"] = False
        out["remote_software"]["enabled"] = False
        out["remote_software"]["blocking_enabled"] = False
        out["web"]["enabled"] = False
    out["web"]["list_version"] = device_list_version(device, policy)
    return out


def blocks_websites(device: Device, policy: Policy, ent: Entitlements | None = None) -> bool:
    """This PC blocks sites: the owner chose Block & watch, the plan has it and the member hasn't declined."""
    return (
        policy.web_enabled
        and policy.web_mode == Policy.WebMode.BLOCK
        and device_entitlements(device, ent).has(plan.WEB)
        and device.consent_state != Device.CONSENT_DECLINED
    )


def device_list_version(device: Device, policy: Policy, ent: Entitlements | None = None) -> str:
    """
    Names the set of sites this PC should have ("" = none). In the policy and on every heartbeat: a new list version
    changes it without the owner touching anything, so the policy version alone wouldn't tell the PC.
    """
    if not blocks_websites(device, policy, ent):
        return ""
    return blocklists.set_version(policy.web_categories, policy.web_allow_domains, policy.web_custom_domains)


def device_block_set(device: Device, policy: Policy) -> dict[str, Any]:
    """GET policy/blocklist/: the sites this PC blocks, by list. Empty when it blocks nothing."""
    if not blocks_websites(device, policy):
        return {"version": "", "sites": {}}
    return {
        "version": device_list_version(device, policy),
        "sites": blocklists.resolve(policy.web_categories, policy.web_allow_domains, policy.web_custom_domains),
    }


# --- USB lockdown / restore (J5-02) ------------------------------------------------------------


def request_usb_restore(owner: User, device: Device) -> Device:
    """The owner pressed "Restore USB": the PC's next heartbeat tells the agent to go back to the policy."""
    if device.owner_id != owner.id:
        raise PermissionError("Not your PC.")
    device.usb_restore_requested_at = timezone.now()
    device.save(update_fields=["usb_restore_requested_at"])
    transaction.on_commit(
        lambda: track("device.usb_restore_requested", {"hostname": device.hostname}, actor_email=owner.email, ref=device)
    )
    return device


def _track_usb_state(device: Device, rows: list[DeviceEvent]) -> None:
    """A lockdown event marks the PC USB-locked; the agent's usb.restored clears it (and the request)."""
    changed: set[str] = set()
    for row in sorted(rows, key=lambda r: r.occurred_at):
        if row.type == USB_LOCKED_DOWN:
            if device.usb_locked_at is None or row.occurred_at > device.usb_locked_at:
                device.usb_locked_at = row.occurred_at
                changed.add("usb_locked_at")
        elif row.type == USB_RESTORED:
            if device.usb_locked_at is None or row.occurred_at >= device.usb_locked_at:
                device.usb_locked_at = None
                device.usb_restore_requested_at = None
                changed |= {"usb_locked_at", "usb_restore_requested_at"}
    if changed:
        device.save(update_fields=sorted(changed))


def check_policy_change(owner: User, data: dict[str, Any]) -> None:
    """Refuse (FeatureLocked, 403) a rule the owner's plan doesn't include."""
    ent = entitlements_for(owner)
    usb = data.get("usb")
    if usb is not None and usb["mode"] != Policy.UsbMode.ALLOW and not ent.has(plan.USB_CONTROL):
        raise plan.FeatureLocked(plan.USB_CONTROL)
    transfer = data.get("data_transfer")
    if transfer is not None and transfer["usb_lockdown"] and not ent.has(plan.USB_CONTROL):
        raise plan.FeatureLocked(plan.USB_CONTROL)
    if data.get("web") is not None and not ent.has(plan.WEB):
        raise plan.FeatureLocked(plan.WEB)
    remote = data.get("remote_software")
    if remote is not None:
        if remote["blocking_enabled"] and not ent.has(plan.REMOTE_ACCESS):
            raise plan.FeatureLocked(plan.REMOTE_ACCESS)
        if not (ent.has(plan.REMOTE_ACCESS) or ent.has(plan.SOFTWARE_INSTALLS)):
            raise plan.FeatureLocked(plan.REMOTE_ACCESS)


def _locked_kinds(field: str, ent: Entitlements) -> Q:
    """A Q matching rows whose event type / metric category belongs to a module `ent` lacks."""
    return plan.locked_kinds_q(field, ent)


# Each policy feature maps its validated block's keys to the model fields they set.
# `(block_key, {wire_key: model_field})` -- a new feature adds one entry here (and
# its block to PolicyUpdateSerializer). update_policy diffs every mapped field and
# bumps the version once if any of them actually changed.
_POLICY_BLOCKS: list[tuple[str, dict[str, str]]] = [
    ("usb", {"mode": "usb_mode"}),
    (
        "web",
        {
            "enabled": "web_enabled",
            "mode": "web_mode",
            "categories": "web_categories",
            "custom_domains": "web_custom_domains",
            "allow_domains": "web_allow_domains",
        },
    ),
    (
        "data_transfer",
        {
            "enabled": "data_transfer_enabled",
            "usb_warn_bytes": "data_transfer_usb_warn_bytes",
            "lan_warn_bytes": "data_transfer_lan_warn_bytes",
            "email_warn_bytes": "data_transfer_email_warn_bytes",
            "web_upload_warn_bytes": "web_upload_warn_bytes",
            "include_details": "data_transfer_include_details",
            "usb_lockdown": "data_transfer_usb_lockdown",
        },
    ),
    (
        "remote_software",
        {
            "enabled": "remote_software_enabled",
            "blocking_enabled": "remote_software_blocking_enabled",
            "blocked_tools": "remote_software_blocked_tools",
            "extra_tool_executables": "remote_software_extra_tool_executables",
        },
    ),
]


@transaction.atomic
def update_policy(owner: User, data: dict[str, Any]) -> Policy:
    """
    Apply a partial policy change, e.g. `{"usb": {"mode": ...}}`. Only the feature
    blocks present in `data` are touched. The version goes up by one when anything
    actually changed, so re-saving the same rules doesn't make every PC re-fetch.

    Each feature maps a validated block to the model fields it changed. A new
    feature adds one entry to `_POLICY_BLOCKS` (and its block to PolicyUpdateSerializer).
    """
    check_policy_change(owner, data)
    get_policy(owner)  # make sure the row exists before locking it
    policy = Policy.objects.select_for_update().get(owner=owner)

    web = data.get("web")
    if web is not None:
        # Own sites come off the allow list, and a hidden name is stored as its id, whichever way it was sent.
        allow = blocklists.clean_allow(web.get("allow_domains", policy.web_allow_domains), web["custom_domains"])
        data = {**data, "web": {**web, "allow_domains": allow}}

    changed: dict[str, Any] = {}
    for block_key, field_map in _POLICY_BLOCKS:
        block = data.get(block_key)
        if block is None:
            continue
        for wire_key, model_field in field_map.items():
            if wire_key not in block:
                continue  # an optional field an older dashboard doesn't send: the saved value stays
            new_value = block[wire_key]
            if getattr(policy, model_field) != new_value:
                setattr(policy, model_field, new_value)
                changed[model_field] = new_value

    if not changed:
        return policy

    policy.version += 1
    policy.save(update_fields=[*changed, "version", "updated_at"])
    transaction.on_commit(
        lambda: track(
            "policy.updated",
            {**changed, "version": policy.version},
            actor_email=owner.email,
            ref=policy,
        )
    )
    return policy


# --- Events -----------------------------------------------------------------


def _stored_client_ids(device: Device, ids: list[Any]) -> set[Any]:
    return set(
        DeviceEvent.objects.filter(device=device, client_id__in=ids).values_list(
            "client_id", flat=True
        )
    )


@transaction.atomic
def store_events(device: Device, events: list[dict[str, Any]]) -> tuple[int, int]:
    """
    Save uploaded agent events idempotently on (device, client_id).

    Returns (accepted, duplicates). Ids already stored -- or repeated within the
    same batch -- count as duplicates, so an agent that re-sends after a lost
    response still gets a 200 and can clear its queue.
    """
    # Plan enforcement: events from a module the plan doesn't include are not kept. They count
    # as accepted (handled) so the agent clears its queue instead of re-sending them forever.
    ent = device_entitlements(device)
    if monitoring_allowed(device):
        kept = [event for event in events if ent.allows(event["type"])]
    else:
        # Declined consent: nothing about this person is kept, only agent housekeeping (agent.* / consent.*).
        kept = [event for event in events if event["type"].split(".", 1)[0] in ("agent", "consent")]
    dropped = len(events) - len(kept)
    events = kept
    seen = _stored_client_ids(device, [event["id"] for event in events])
    rows: list[DeviceEvent] = []
    for event in events:
        if event["id"] in seen:
            continue
        seen.add(event["id"])
        rows.append(
            DeviceEvent(
                device=device,
                client_id=event["id"],
                type=event["type"],
                severity=event["severity"],
                occurred_at=event["occurred_at"],
                data=event["data"],
            )
        )

    try:
        with transaction.atomic():
            DeviceEvent.objects.bulk_create(rows)
        accepted = len(rows)
    except IntegrityError:
        # A concurrent upload of the same ids won the race: fall back to one by one.
        accepted = 0
        for row in rows:
            try:
                with transaction.atomic():
                    row.save(force_insert=True)
                accepted += 1
            except IntegrityError:
                pass
    stored = [row for row in rows if row.pk is not None] if accepted != len(rows) else rows
    _track_usb_state(device, stored)
    critical = [row for row in stored if row.severity == DeviceEvent.Severity.CRITICAL and not is_confirmation(row.type)]
    if critical:
        transaction.on_commit(lambda: tell_owner_critical(device, critical))
    web_push.announce(device.owner_id, "alert", [row.pk for row in stored])
    return accepted + dropped, len(events) - accepted


OWNER_ALERT_TEMPLATE = "owner_alert"
OWNER_ALERT_EVERY = timedelta(minutes=30)


def tell_owner_critical(device: Device, events: list[DeviceEvent]) -> None:
    """
    Email the owner about critical alerts (J5-21), at most one email every 30 minutes per owner so a burst
    doesn't flood the inbox (the dashboard has the full list). Never raises.
    """
    from z_notification.models import Notification

    owner = device.owner
    recent = Notification.objects.filter(
        template=OWNER_ALERT_TEMPLATE, recipient=owner.email, created_at__gte=timezone.now() - OWNER_ALERT_EVERY,
    ).exists()
    if recent:
        return
    member = device.member if device.member_id else None
    notify(
        "email",
        owner.email,
        OWNER_ALERT_TEMPLATE,
        {
            "first_name": _first_name(owner.full_name or ""),
            "hostname": device.hostname,
            "member_name": member.name if member else "",
            "titles": [build_title(e.type, e.data, device.hostname) for e in events][:5],
            "count": len(events),
            "first": events[0],
            "alerts_url": f"{settings.ZINWE_APP_URL}/app/alerts",
        },
        ref=events[0],
    )


def _history_floor(ent: Entitlements, since: Any) -> Any:
    """`since`, moved forward to the start of the plan's history window when the plan keeps less."""
    floor = ent.history_since()
    return max(since, floor) if floor is not None else since


def events_for_owner(
    owner: User,
    device_id: int | None = None,
    limit: int = 50,
    *,
    member_id: int | None = None,
    types: list[str] | None = None,
    kind: str | None = None,
    handled: bool | None = None,
) -> QuerySet:
    """
    The owner's newest events first (within their plan's history), optionally for one of their devices,
    one team member's PCs, only some event types, only alerts or only rule confirmations (`kind`), and/or
    only handled / not-handled ones.
    """
    ent = entitlements_for(owner)
    events = DeviceEvent.objects.filter(device__owner=owner).exclude(_locked_kinds("type", ent)).select_related("device")
    since = ent.history_since()
    if since is not None:
        events = events.filter(occurred_at__gte=since)
    if device_id is not None:
        events = events.filter(device_id=device_id)
    if member_id is not None:
        events = events.filter(device__member_id=member_id)
    if types:
        events = events.filter(type__in=types)
    if kind:
        confirmations = confirmation_q()
        events = events.filter(confirmations) if kind == "confirmation" else events.exclude(confirmations)
    if handled is not None:
        events = events.filter(handled_at__isnull=not handled)
    return events.order_by("-occurred_at", "-id")[:limit]


def confirmation_q(field: str = "type") -> Q:
    """Rule confirmations (kind "confirmation"), for excluding them from alert lists and counts."""
    from .events import CONFIRMATION_TYPES

    return Q(**{f"{field}__in": sorted(CONFIRMATION_TYPES)}) | Q(**{f"{field}__endswith": ".policy_applied"})


def alert_counts(owner: User, since: Any = None) -> dict[str, int]:
    """How many alerts (confirmations excluded) the owner has, and how many still need attention."""
    ent = entitlements_for(owner)
    events = DeviceEvent.objects.filter(device__owner=owner).exclude(_locked_kinds("type", ent)).exclude(confirmation_q())
    floor = ent.history_since()
    if since is not None:
        floor = max(since, floor) if floor is not None else since
    if floor is not None:
        events = events.filter(occurred_at__gte=floor)
    return {
        "alerts": events.count(),
        "unhandled": events.filter(handled_at__isnull=True).count(),
        "critical_unhandled": events.filter(handled_at__isnull=True, severity=DeviceEvent.Severity.CRITICAL).count(),
    }


# --- Usage metrics ----------------------------------------------------------


def store_metrics(device: Device, rows: list[dict[str, Any]]) -> int:
    """
    Upsert hour-bucketed usage rows for `device`, REPLACING counts (not adding).

    The agent re-sends the still-accumulating current hour each round with growing
    totals, so count/bytes/seconds are SET to the row's values on (device,
    bucket_start, category, key). Returns the number of rows accepted.
    """
    ent = entitlements_for(device.owner)
    if not monitoring_allowed(device):
        return len(rows)  # declined consent: usage isn't collected; handled so the agent clears its queue
    accepted = 0
    for row in rows:
        if not ent.allows(row["category"]):
            accepted += 1  # plan enforcement: not kept, but handled (the agent clears it)
            continue
        with transaction.atomic():
            UsageMetric.objects.update_or_create(
                device=device,
                bucket_start=row["bucket_start"],
                category=row["category"],
                key=row.get("key", ""),
                defaults={
                    "count": row.get("count", 0),
                    "bytes": row.get("bytes", 0),
                    "seconds": row.get("seconds", 0),
                },
            )
        accepted += 1
    return accepted


def metrics_for_owner(
    owner: User,
    *,
    member_id: int | None = None,
    device_id: int | None = None,
    days: int = 7,
    category: str | None = None,
) -> dict[str, Any]:
    """
    Aggregate the owner's own devices' usage for the dashboard charts.

    Returns `{"categories": [...totals per category...], "series": [...per bucket...]}`,
    scoped to `owner`'s devices only, over the last `days` days (by bucket_start),
    optionally filtered to one member, one device, and/or one category. The series
    is ordered newest-last (oldest bucket first) so a chart can plot it directly.
    """
    ent = entitlements_for(owner)
    since = _history_floor(ent, timezone.now() - timedelta(days=days))
    qs = UsageMetric.objects.filter(device__owner=owner, bucket_start__gte=since).exclude(
        _locked_kinds("category", ent)
    )
    if member_id is not None:
        qs = qs.filter(device__member_id=member_id)
    if device_id is not None:
        qs = qs.filter(device_id=device_id)
    if category:
        qs = qs.filter(category=category)

    categories = list(
        qs.values("category")
        .annotate(
            total_count=Sum("count"),
            total_bytes=Sum("bytes"),
            total_seconds=Sum("seconds"),
        )
        .order_by("category")
    )
    # Collapse to one row per (bucket, category) across the owner's devices, so a
    # member/owner with several PCs charts a single combined series.
    series = list(
        qs.values("bucket_start", "category")
        .annotate(
            count=Sum("count"),
            bytes=Sum("bytes"),
            seconds=Sum("seconds"),
        )
        .order_by("bucket_start", "category")
    )
    return {"categories": categories, "series": series}


def top_metrics_for_owner(
    owner: User,
    *,
    category: str,
    member_id: int | None = None,
    device_id: int | None = None,
    days: int = 7,
    limit: int = 20,
) -> dict[str, Any]:
    """
    The per-KEY breakdown within one category for the owner's own devices -- e.g.
    the top apps ("app.foreground") or top websites ("web.domain"), each with its
    summed time/visits/bytes, ranked most-used first.

    This is the detail `metrics_for_owner` collapses away. Owner-scoped like every
    read here, so a company only ever sees its own PCs. Blank-key rows (metrics
    with no per-item detail) are left out -- there's nothing to break down.
    """
    ent = entitlements_for(owner)
    since = _history_floor(ent, timezone.now() - timedelta(days=days))
    qs = (
        UsageMetric.objects.filter(device__owner=owner, bucket_start__gte=since, category=category)
        .exclude(key="")
        .exclude(_locked_kinds("category", ent))
    )
    if member_id is not None:
        qs = qs.filter(device__member_id=member_id)
    if device_id is not None:
        qs = qs.filter(device_id=device_id)

    items = list(
        qs.values("key")
        .annotate(count=Sum("count"), bytes=Sum("bytes"), seconds=Sum("seconds"))
        # Rank by whichever measure the category uses: time first (apps/sites), then
        # bytes (transfers/uploads, whose seconds are 0), then count as a tiebreak.
        .order_by("-seconds", "-bytes", "-count", "key")[:limit]
    )
    return {"category": category, "items": items}
