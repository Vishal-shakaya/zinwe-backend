"""
Business logic for z_devices. Views stay thin and call into here.

Side effects (analytics) are scheduled with `transaction.on_commit`, as in z_auth.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import QuerySet, Sum
from django.utils import timezone

from z_analytics.services import track
from z_auth.models import User
from z_notification.services import notify

from .events import build_title
from .models import (
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


def mark_read_by_member(device: Device) -> int:
    """Mark the admin's messages to this member as seen by the member. Returns how many."""
    if device.member_id is None:
        return 0
    return Message.objects.filter(
        member_id=device.member_id, sender=Message.Sender.OWNER, read_by_member=False
    ).update(read_by_member=True)


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
    reason = (data.get("break_reason") or "").strip()[:120] if new_state == Device.WorkState.ON_BREAK else ""
    return WorkLog.objects.create(
        owner_id=device.owner_id, device=device, member_id=device.member_id,
        state=new_state, previous_state=device.work_state or "", at=at, reason=reason,
    )


def work_log_for_owner(owner: User, day: Any, member_id: int | None = None) -> QuerySet:
    """The owner's work-day changes on one local calendar day, oldest first."""
    logs = WorkLog.objects.filter(owner=owner, at__date=day).select_related("device", "member")
    if member_id is not None:
        logs = logs.filter(member_id=member_id)
    return logs.order_by("at", "id")


def record_heartbeat(device: Device, data: dict[str, Any], ip: str | None) -> dict[str, Any]:
    """Mark the device as seen, store its work status, and tell the agent when to call back."""
    now = timezone.now()
    device.last_seen_at = now
    device.last_ip = ip
    for field in ("agent_version", "os_version", "hostname"):
        if data.get(field):
            setattr(device, field, data[field])
    # Work status (M6). The serializer supplies sane defaults for an older agent
    # that omits these, so validated_data always carries them.
    _log_work_change(device, data, now)
    device.work_state = data["work_state"]
    device.worked_seconds = data["worked_seconds"]
    device.break_seconds = data["break_seconds"]
    device.work_updated_at = now
    device.save(
        update_fields=[
            "last_seen_at", "last_ip", "agent_version", "os_version", "hostname",
            "work_state", "worked_seconds", "break_seconds", "work_updated_at",
        ]
    )
    response = {
        "device_id": device.id,
        "server_time": device.last_seen_at,
        "next_heartbeat_seconds": settings.ZINWE_DEVICE_HEARTBEAT_SECONDS,
        # The agent re-fetches GET policy/current/ whenever this differs from its cache.
        "policy_version": get_policy(device.owner).version,
    }
    # Forced upgrade (M12): tell an out-of-date agent to update and where to get it.
    # Keys are omitted entirely when no update is due, so the response shape is unchanged.
    if update_required(device.agent_version):
        response["update_required"] = True
        response["download_url"] = settings.ZINWE_AGENT_DOWNLOAD_URL
    return response


def _parse_version(value: str) -> tuple[int, ...]:
    """Dotted numeric version to a tuple; stops at the first non-numeric part ("" -> ())."""
    parts: list[int] = []
    for chunk in (value or "").split("."):
        chunk = chunk.strip()
        if not chunk.isdigit():
            break
        parts.append(int(chunk))
    return tuple(parts)


def update_required(agent_version: str) -> bool:
    """True when `agent_version` is below the configured minimum ("" minimum = never)."""
    minimum = _parse_version(settings.ZINWE_AGENT_MIN_VERSION)
    if not minimum:
        return False
    current = _parse_version(agent_version)
    length = max(len(current), len(minimum))
    current += (0,) * (length - len(current))
    minimum += (0,) * (length - len(minimum))
    return current < minimum


# --- Policy -----------------------------------------------------------------


def get_policy(owner: User) -> Policy:
    """The owner's policy, created on first use (version 1, USB allowed)."""
    policy, _ = Policy.objects.get_or_create(owner=owner)
    return policy


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
    return accepted, len(events) - accepted


def events_for_owner(owner: User, device_id: int | None = None, limit: int = 50) -> QuerySet:
    """The owner's newest events first, optionally for one of their devices."""
    events = DeviceEvent.objects.filter(device__owner=owner).select_related("device")
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
    accepted = 0
    for row in rows:
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
    qs = UsageMetric.objects.filter(device__owner=owner, bucket_start__gte=since)
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
    qs = UsageMetric.objects.filter(
        device__owner=owner, bucket_start__gte=since, category=category
    ).exclude(key="")
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
