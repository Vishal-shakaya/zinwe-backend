"""
Business logic for z_devices. Views stay thin and call into here.

Side effects (analytics) are scheduled with `transaction.on_commit`, as in z_auth.
"""
from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.utils import timezone

from z_analytics.services import track
from z_auth.models import User

from .models import Device, DeviceEvent, EnrollmentKey, Policy, hash_device_token


class EnrollmentRejected(Exception):
    """The enrollment key is unknown, revoked, or its owner is disabled."""


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


# --- Agent lifecycle --------------------------------------------------------


@transaction.atomic
def enroll_device(data: dict[str, Any], ip: str | None) -> tuple[Device, str, bool]:
    """
    Join a PC to the owner of `data["enrollment_key"]`.

    Returns (device, raw_token, created). Re-enrolling the same machine for the
    same owner (agent reinstall) rotates its token and reactivates it rather
    than creating a duplicate row. The raw token is returned exactly once.
    """
    key = (
        EnrollmentKey.objects.select_for_update()
        .select_related("owner")
        .filter(key=data["enrollment_key"], is_active=True)
        .first()
    )
    if key is None or not key.owner.is_active:
        raise EnrollmentRejected("This enrollment key is not valid. Ask your admin for a new one.")

    now = timezone.now()
    token = Device.new_token()
    device, created = Device.objects.update_or_create(
        owner=key.owner,
        machine_id=data["machine_id"],
        defaults={
            "enrollment_key": key,
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
    key.last_used_at = now
    key.save(update_fields=["last_used_at"])

    transaction.on_commit(
        lambda: track(
            "device.enrolled",
            {"hostname": device.hostname, "platform": device.platform, "re_enrolled": not created},
            actor_email=key.owner.email,
            ref=device,
        )
    )
    return device, token, created


def record_heartbeat(device: Device, data: dict[str, Any], ip: str | None) -> dict[str, Any]:
    """Mark the device as seen and tell the agent when to call back."""
    device.last_seen_at = timezone.now()
    device.last_ip = ip
    for field in ("agent_version", "os_version", "hostname"):
        if data.get(field):
            setattr(device, field, data[field])
    device.save(update_fields=["last_seen_at", "last_ip", "agent_version", "os_version", "hostname"])
    return {
        "device_id": device.id,
        "server_time": device.last_seen_at,
        "next_heartbeat_seconds": settings.ZINWE_DEVICE_HEARTBEAT_SECONDS,
        # The agent re-fetches GET policy/current/ whenever this differs from its cache.
        "policy_version": get_policy(device.owner).version,
    }


# --- Policy -----------------------------------------------------------------


def get_policy(owner: User) -> Policy:
    """The owner's policy, created on first use (version 1, USB allowed)."""
    policy, _ = Policy.objects.get_or_create(owner=owner)
    return policy


@transaction.atomic
def update_policy(owner: User, data: dict[str, Any]) -> Policy:
    """
    Apply `{"usb": {"mode": ...}}`. The version goes up by one only when a rule
    actually changes, so re-saving the same rules doesn't make every PC re-fetch.
    """
    get_policy(owner)  # make sure the row exists before locking it
    policy = Policy.objects.select_for_update().get(owner=owner)
    usb_mode = data["usb"]["mode"]
    if policy.usb_mode == usb_mode:
        return policy

    policy.usb_mode = usb_mode
    policy.version += 1
    policy.save(update_fields=["usb_mode", "version", "updated_at"])
    transaction.on_commit(
        lambda: track(
            "policy.updated",
            {"usb_mode": policy.usb_mode, "version": policy.version},
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
