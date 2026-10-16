"""
Business logic for z_devices. Views stay thin and call into here.

Side effects (analytics) are scheduled with `transaction.on_commit`, as in z_auth.
"""
from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Q, QuerySet, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from z_analytics.services import track
from z_auth import entitlements as plan
from z_auth.entitlements import Entitlements, entitlements_for
from z_auth.models import User
from z_notification.services import notify

from .events import build_title
from .models import (
    AgentRelease,
    Device,
    DeviceEvent,
    EnrollmentKey,
    Member,
    Message,
    Policy,
    UsageMetric,
    WorkLog,
    generate_member_code,
    hash_device_token,
)

# How many of a member's recent messages the agent is handed per poll.
MESSAGE_FEED_LIMIT = 50


class EnrollmentRejected(Exception):
    """
    The agent's credentials don't let it join. `field` is the response key the
    agent keys its reaction off (see the contract): "member_id"/"email" mean a
    bad credential, "non_field_errors" means the member/owner is switched off.
    """

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


class MemberAlreadyExists(Exception):
    """This owner already registered a member with this email."""


# --- Enrollment keys --------------------------------------------------------


def get_active_key(owner: User) -> EnrollmentKey:
    """The owner's active key, created on first use."""
    key = owner.enrollment_keys.filter(is_active=True).first()
    return key or EnrollmentKey.objects.create(owner=owner)


@transaction.atomic
def rotate_key(owner: User) -> EnrollmentKey:
    """Revoke the active key (enrolled devices keep working) and issue a new one."""
    owner.enrollment_keys.filter(is_active=True).update(is_active=False, revoked_at=timezone.now())
    key = EnrollmentKey.objects.create(owner=owner)
    transaction.on_commit(
        lambda: track("devices.key_rotated", {}, actor_email=owner.email, ref=key)
    )
    return key


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
def create_member(owner: User, name: str, email: str) -> Member:
    """
    Register an employee under `owner` and mint their enrol id.

    Raises MemberAlreadyExists if this owner already has a member with this email.
    """
    email = _normalise_email(email)
    if owner.members.filter(email=email).exists():
        raise MemberAlreadyExists("A member with this email already exists.")
    try:
        member = Member.objects.create(
            owner=owner, name=name.strip(), email=email, member_code=generate_unique_member_code()
        )
    except IntegrityError as exc:  # the unique (owner, email) lost a race
        raise MemberAlreadyExists("A member with this email already exists.") from exc

    transaction.on_commit(lambda: _after_member_created(owner, member))
    return member


def _after_member_created(owner: User, member: Member) -> None:
    """Post-commit: track the event and email the member their id + the download link. Never raises."""
    track("member.created", {"email": member.email}, actor_email=owner.email, ref=member)
    parts = member.name.split()
    company = (owner.company or "").strip() or "your company"
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
        },
        ref=member,
        redact=[member.member_code],  # the id is the enrol secret: masked in our logs, sent to the member
    )


@transaction.atomic
def rotate_member_code(member: Member) -> Member:
    """Issue a fresh id for this member; the old one stops working immediately."""
    member.member_code = generate_unique_member_code()
    member.save(update_fields=["member_code"])
    transaction.on_commit(
        lambda: track(
            "member.id_rotated", {"email": member.email}, actor_email=member.owner.email, ref=member
        )
    )
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


@transaction.atomic
def _company_name(owner: User) -> str:
    company = getattr(owner, "owned_company", None)
    return (getattr(company, "name", "") or owner.company or "").strip() or "your company"


def _first_name(name: str) -> str:
    parts = name.split()
    return parts[0] if parts else "there"


def send_message(
    owner: User, member: Member, title: str, body: str, severity: str, also_email: bool = False
) -> Message:
    """
    Send `member` a short message that will surface on their widget.

    `member` must belong to `owner` (the view resolves it from the owner's own
    members, so this is a defensive re-check). On commit, track the send and,
    with `also_email`, email the same note to the member (so it reaches them
    even when their PC is off).
    """
    if member.owner_id != owner.id:
        raise ValueError("Member does not belong to this owner.")

    message = Message.objects.create(
        owner=owner, member=member, sender=Message.Sender.OWNER,
        title=title, body=body, severity=severity,
        read_by_owner=True,  # the admin wrote it
    )
    transaction.on_commit(
        lambda: track(
            "member.message_sent",
            {"member_id": member.id, "severity": severity, **({"emailed": True} if also_email else {})},
            actor_email=owner.email,
            ref=message,
        )
    )
    if also_email:
        transaction.on_commit(lambda: _email_message(owner, member, message))
    return message


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
    return message


def messages_for_device(device: Device) -> QuerySet:
    """
    The device's member's conversation (both directions), newest first (empty if
    unlinked). Returns a QuerySet so an unlinked PC gets a cheap empty result.
    """
    if device.member_id is None:
        return Message.objects.none()
    return Message.objects.filter(member_id=device.member_id).order_by(
        "-created_at", "-id"
    )[:MESSAGE_FEED_LIMIT]


def mark_delivered(device: Device) -> int:
    """
    Stamp the admin's messages as delivered: the member's PC has pulled them (called on the
    agent's poll). Only undelivered rows are touched, so a poll with nothing new writes nothing.
    """
    if device.member_id is None:
        return 0
    return Message.objects.filter(
        member_id=device.member_id, sender=Message.Sender.OWNER, delivered_at__isnull=True
    ).update(delivered_at=timezone.now())


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
    ).update(
        read_by_member=True,
        read_by_member_at=now,
        delivered_at=Coalesce(F("delivered_at"), Value(now)),
    )


def thread_for_owner(owner: User, member: Member) -> QuerySet:
    """The full conversation with one member (both directions), newest first. Owner-scoped."""
    return Message.objects.filter(owner=owner, member=member).order_by("-created_at", "-id")[:MESSAGE_FEED_LIMIT]


def mark_thread_read_by_owner(member: Member) -> int:
    """Mark the member's replies as seen by the admin (called when the admin opens the chat)."""
    return Message.objects.filter(
        member=member, sender=Message.Sender.MEMBER, read_by_owner=False
    ).update(read_by_owner=True)


def unread_from_member(member: Member) -> int:
    """How many of this member's replies the admin hasn't seen yet (for the Team list badge)."""
    return Message.objects.filter(
        member=member, sender=Message.Sender.MEMBER, read_by_owner=False
    ).count()


def current_device_for(member: Member) -> Device | None:
    """The member's most recently seen PC (prefetched devices sorted in Python)."""
    devices = list(member.devices.all())
    if not devices:
        return None
    return max(
        devices,
        key=lambda d: (d.last_seen_at or d.enrolled_at, d.id),
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
    )


def _record_consent(device: Device, data: dict[str, Any], now: Any) -> None:
    """Keep the member's consent (version + when) from the widget; an implausible time becomes now."""
    version = (data.get("consent_version") or "").strip()[:20]
    if not version or version == device.consent_version:
        return
    at = data.get("consent_at") or now
    if at > now:
        at = now
    device.consent_version = version
    device.consent_at = at


LOGGED_WORK_STATES = {Device.WorkState.WORKING, Device.WorkState.ON_BREAK, Device.WorkState.ENDED}


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
    reason = (data.get(f"{prefix}_reason") or "").strip()[:120] if prefix else ""
    note = (data.get(f"{prefix}_note") or "").strip()[:280] if prefix else ""
    return WorkLog.objects.create(
        owner_id=device.owner_id, device=device, member_id=device.member_id,
        state=new_state, previous_state=device.work_state or "", at=at, reason=reason, note=note,
        approval=WorkLog.Approval.PENDING if on_break else "",
    )


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
        previous = (
            WorkLog.objects.filter(device=device, at__lt=at)
            .order_by("-at", "-id")
            .values_list("state", flat=True)
            .first()
        ) or ""
        if previous == state:
            continue
        on_break = state == Device.WorkState.ON_BREAK
        explained = state in (Device.WorkState.ON_BREAK, Device.WorkState.ENDED)
        WorkLog.objects.create(
            owner_id=device.owner_id, device=device, member_id=device.member_id,
            state=state, previous_state=previous, at=at,
            reason=(event.get("reason") or "").strip()[:120] if explained else "",
            note=(event.get("note") or "").strip()[:280] if explained else "",
            approval=WorkLog.Approval.PENDING if on_break else "",
        )
        added += 1
    return added


class NotABreak(Exception):
    """Only breaks can be approved or denied."""


def decide_break(owner: User, log: WorkLog, decision: str, note: str = "") -> WorkLog:
    """
    Approve or deny a break (after the fact: the member isn't blocked). The member is told
    in their Zinwe widget, with the owner's note if any. Deciding again overwrites.
    """
    if log.owner_id != owner.id:
        raise PermissionError("Not your team's break.")
    if log.state != WorkLog.State.ON_BREAK:
        raise NotABreak()
    log.approval = decision
    log.decision_note = note.strip()[:280]
    log.decided_at = timezone.now()
    log.save(update_fields=["approval", "decision_note", "decided_at"])
    if log.member_id and log.member and log.member.is_active:
        what = f"{log.reason} break" if log.reason else "break"
        when = timezone.localtime(log.at).strftime("%I:%M %p").lstrip("0")
        approved = decision == WorkLog.Approval.APPROVED
        send_message(
            owner, log.member,
            title=(f"Your {what} at {when} was approved" if approved else f"Your {what} at {when} wasn't approved")[:120],
            body=log.decision_note,
            severity=Message.Severity.INFO if approved else Message.Severity.WARNING,
        )
    return log


def work_log_for_owner(owner: User, day: Any, member_id: int | None = None) -> QuerySet:
    """The owner's work-day changes on one local calendar day, oldest first."""
    logs = WorkLog.objects.filter(owner=owner, at__date=day).select_related("device", "member")
    if member_id is not None:
        logs = logs.filter(member_id=member_id)
    return logs.order_by("at", "id")


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
    if not ent.has(plan.WORK_TIME):
        pass
    elif data.get("work_events") is not None:
        _log_work_events(device, data["work_events"], now)
    else:
        _log_work_change(device, data, now)
    _record_consent(device, data, now)
    if ent.has(plan.WORK_TIME):
        device.work_state = data["work_state"]
        device.worked_seconds = data["worked_seconds"]
        device.break_seconds = data["break_seconds"]
    else:
        # Plan enforcement: no work hours on this plan, so the PC's work day isn't kept either
        # (the dashboard shows it as plain online/offline).
        device.work_state = Device.WorkState.NOT_STARTED
        device.worked_seconds = 0
        device.break_seconds = 0
    device.work_updated_at = now
    device.save(
        update_fields=[
            "last_seen_at", "last_ip", "agent_version", "os_version", "hostname",
            "work_state", "worked_seconds", "break_seconds", "work_updated_at",
            "consent_version", "consent_at",
        ]
    )
    response = {
        "device_id": device.id,
        "server_time": device.last_seen_at,
        "next_heartbeat_seconds": settings.ZINWE_DEVICE_HEARTBEAT_SECONDS,
        # The agent re-fetches GET policy/current/ whenever this differs from its cache.
        "policy_version": effective_policy_version(get_policy(device.owner), ent),
    }
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
        },
    ),
    (
        "data_transfer",
        {
            "enabled": "data_transfer_enabled",
            "usb_warn_bytes": "data_transfer_usb_warn_bytes",
            "lan_warn_bytes": "data_transfer_lan_warn_bytes",
            "email_warn_bytes": "data_transfer_email_warn_bytes",
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

    changed: dict[str, Any] = {}
    for block_key, field_map in _POLICY_BLOCKS:
        block = data.get(block_key)
        if block is None:
            continue
        for wire_key, model_field in field_map.items():
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
    ent = entitlements_for(device.owner)
    kept = [event for event in events if ent.allows(event["type"])]
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
    return accepted + dropped, len(events) - accepted


def events_for_owner(owner: User, device_id: int | None = None, limit: int = 50) -> QuerySet:
    """The owner's newest events first, optionally for one of their devices."""
    events = (
        DeviceEvent.objects.filter(device__owner=owner)
        .exclude(_locked_kinds("type", entitlements_for(owner)))
        .select_related("device")
    )
    if device_id is not None:
        events = events.filter(device_id=device_id)
    return events.order_by("-occurred_at", "-id")[:limit]


# --- Usage metrics ----------------------------------------------------------


def store_metrics(device: Device, rows: list[dict[str, Any]]) -> int:
    """
    Upsert hour-bucketed usage rows for `device`, REPLACING counts (not adding).

    The agent re-sends the still-accumulating current hour each round with growing
    totals, so count/bytes/seconds are SET to the row's values on (device,
    bucket_start, category, key). Returns the number of rows accepted.
    """
    ent = entitlements_for(device.owner)
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
    since = timezone.now() - timedelta(days=days)
    qs = UsageMetric.objects.filter(device__owner=owner, bucket_start__gte=since).exclude(
        _locked_kinds("category", entitlements_for(owner))
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
    since = timezone.now() - timedelta(days=days)
    qs = (
        UsageMetric.objects.filter(device__owner=owner, bucket_start__gte=since, category=category)
        .exclude(key="")
        .exclude(_locked_kinds("category", entitlements_for(owner)))
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
