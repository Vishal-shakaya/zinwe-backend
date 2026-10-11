"""Enrollment keys, endpoint devices (PCs running the Zinwe agent), their policy and events."""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

# Crockford-ish alphabet: no 0/O/1/I/L, so keys survive being read out over the phone.
_KEY_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def generate_enrollment_key() -> str:
    """`ZNW-XXXX-XXXX-XXXX` -- 12 random chars (~59 bits)."""
    groups = ["".join(secrets.choice(_KEY_ALPHABET) for _ in range(4)) for _ in range(3)]
    return "ZNW-" + "-".join(groups)


def generate_member_code() -> str:
    """
    `ZM-XXXX-XXXX` -- 8 chars from the Crockford-ish alphabet (~40 bits).

    This is the per-member enrol secret the owner hands to one employee: unguessable
    enough to stand in for a password, short enough to type off a screen.
    """
    groups = ["".join(secrets.choice(_KEY_ALPHABET) for _ in range(4)) for _ in range(2)]
    return "ZM-" + "-".join(groups)


def hash_device_token(token: str) -> str:
    """Device tokens are stored as SHA-256 hex; the raw token only ever lives on the PC."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class EnrollmentKey(models.Model):
    """
    A reusable key the business owner pastes into the agent installer.

    One active key per owner; rotating revokes the old one. Devices that already
    enrolled keep working -- the key is only needed to join.
    """

    owner = models.ForeignKey(
        "z_auth.User", on_delete=models.CASCADE, related_name="enrollment_keys"
    )
    key = models.CharField(max_length=32, unique=True, default=generate_enrollment_key)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["owner", "is_active"])]

    def __str__(self) -> str:
        return f"{self.key} ({self.owner})"


class Member(models.Model):
    """
    One employee the owner has registered. The owner adds a member (name + email)
    on the dashboard and gets back `member_code` -- the id handed to that employee's
    PC installer. The code (plus the matching email) is what the agent enrols with,
    replacing the old org-wide enrollment key.
    """

    owner = models.ForeignKey("z_auth.User", on_delete=models.CASCADE, related_name="members")
    name = models.CharField(max_length=150)
    email = models.EmailField()
    member_code = models.CharField(max_length=16, unique=True, default=generate_member_code)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # One member per email per owner, case-insensitively (emails are stored
            # lowercased, so a plain-field unique constraint is enough).
            models.UniqueConstraint(fields=["owner", "email"], name="uniq_member_email_per_owner")
        ]
        indexes = [models.Index(fields=["owner", "is_active"])]

    def __str__(self) -> str:
        return f"{self.name} <{self.email}> ({self.member_code})"


class Device(models.Model):
    """One Windows PC running the agent, owned by the business account that enrolled it."""

    class Platform(models.TextChoices):
        WINDOWS = "windows", "Windows"
        LINUX = "linux", "Linux"
        MACOS = "macos", "macOS"

    class WorkState(models.TextChoices):
        """
        Where the member is in their work day, reported on each heartbeat.

        Drives the owner's status column (working / on break / offline...). The
        agent is the source of truth; we just store the latest value.
        """

        NOT_STARTED = "not_started", "Not started"
        WORKING = "working", "Working"
        ON_BREAK = "on_break", "On break"
        ENDED = "ended", "Ended"

    owner = models.ForeignKey("z_auth.User", on_delete=models.CASCADE, related_name="devices")
    enrollment_key = models.ForeignKey(
        EnrollmentKey, null=True, blank=True, on_delete=models.SET_NULL, related_name="devices"
    )
    member = models.ForeignKey(
        Member, null=True, blank=True, on_delete=models.SET_NULL, related_name="devices"
    )
    machine_id = models.CharField(
        max_length=64, help_text="Stable OS install id (Windows MachineGuid)."
    )
    hostname = models.CharField(max_length=255)
    platform = models.CharField(max_length=16, choices=Platform.choices, default=Platform.WINDOWS)
    os_version = models.CharField(max_length=128, blank=True)
    agent_version = models.CharField(max_length=32, blank=True)
    token_hash = models.CharField(max_length=64, unique=True)
    is_revoked = models.BooleanField(default=False)
    enrolled_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    last_ip = models.GenericIPAddressField(null=True, blank=True)

    # Work status, carried on every heartbeat (M6). `work_updated_at` is when the
    # agent last reported it, so the owner can tell a fresh "on break" from a stale one.
    work_state = models.CharField(
        max_length=16, choices=WorkState.choices, default=WorkState.NOT_STARTED
    )
    worked_seconds = models.PositiveIntegerField(default=0)
    break_seconds = models.PositiveIntegerField(default=0)
    work_updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["hostname"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "machine_id"], name="uniq_device_per_owner")
        ]

    def __str__(self) -> str:
        return f"{self.hostname} ({self.owner})"

    @staticmethod
    def new_token() -> str:
        return secrets.token_urlsafe(32)

    @property
    def is_online(self) -> bool:
        """Seen within ZINWE_DEVICE_OFFLINE_AFTER_SECONDS (3 missed heartbeats by default)."""
        if self.last_seen_at is None or self.is_revoked:
            return False
        window = timedelta(seconds=settings.ZINWE_DEVICE_OFFLINE_AFTER_SECONDS)
        return timezone.now() - self.last_seen_at <= window


class Policy(models.Model):
    """
    The protection rules an owner's PCs enforce. One per owner, created lazily.

    `version` goes up by one every time a rule actually changes; agents compare it
    with the `policy_version` in their heartbeat and re-fetch when it differs.
    """

    class UsbMode(models.TextChoices):
        ALLOW = "allow", "Allow"
        READ_ONLY = "read_only", "Read-only"
        BLOCK = "block", "Block"

    class WebMode(models.TextChoices):
        MONITOR = "monitor", "Monitor"  # record usage only
        BLOCK = "block", "Block"        # record usage and block the list

    # Warn-size defaults: 50 MiB for USB/LAN, 20 MiB for email attachments.
    DEFAULT_USB_WARN_BYTES = 52428800
    DEFAULT_LAN_WARN_BYTES = 52428800
    DEFAULT_EMAIL_WARN_BYTES = 20971520

    owner = models.OneToOneField(
        "z_auth.User", on_delete=models.CASCADE, related_name="device_policy"
    )
    version = models.PositiveIntegerField(default=1)
    usb_mode = models.CharField(max_length=16, choices=UsbMode.choices, default=UsbMode.ALLOW)

    # Web usage / blocking. Monitor + empty lists = safe default (watch, block nothing).
    web_enabled = models.BooleanField(default=True)
    web_mode = models.CharField(max_length=16, choices=WebMode.choices, default=WebMode.MONITOR)
    web_categories = models.JSONField(default=list, blank=True)
    web_custom_domains = models.JSONField(default=list, blank=True)

    # Data-transfer visibility. Details off by default (DPDP / consent-first).
    data_transfer_enabled = models.BooleanField(default=True)
    data_transfer_usb_warn_bytes = models.PositiveBigIntegerField(default=DEFAULT_USB_WARN_BYTES)
    data_transfer_lan_warn_bytes = models.PositiveBigIntegerField(default=DEFAULT_LAN_WARN_BYTES)
    data_transfer_email_warn_bytes = models.PositiveBigIntegerField(
        default=DEFAULT_EMAIL_WARN_BYTES
    )
    data_transfer_include_details = models.BooleanField(default=False)
    # When on, a USB copy over the usb limit auto-sets the PC to USB read-only (agent enforces).
    data_transfer_usb_lockdown = models.BooleanField(default=False)

    # Remote-software detection. Always detects; blocking is opt-in.
    remote_software_enabled = models.BooleanField(default=True)
    remote_software_blocking_enabled = models.BooleanField(default=False)
    remote_software_blocked_tools = models.JSONField(default=list, blank=True)
    remote_software_extra_tool_executables = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "policies"

    def __str__(self) -> str:
        return f"Policy v{self.version} ({self.owner})"


class Message(models.Model):
    """
    A short note the owner sends one member; it surfaces on that member's widget.

    The agent polls GET /api/devices/messages/ after each heartbeat and dedupes by
    `id`, so we simply return the member's recent messages, newest first. `owner` is
    denormalised off the member for simple, index-backed owner-scoped queries and so
    the row survives even if the member FK is later repointed.
    """

    class Severity(models.TextChoices):
        INFO = "info", "Info"
        WARNING = "warning", "Warning"
        CRITICAL = "critical", "Critical"

    class Sender(models.TextChoices):
        OWNER = "owner", "Admin"
        MEMBER = "member", "Member"

    owner = models.ForeignKey(
        "z_auth.User", on_delete=models.CASCADE, related_name="sent_messages"
    )
    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="messages")
    # Who wrote this line of the conversation (admin alert vs member reply).
    sender = models.CharField(max_length=10, choices=Sender.choices, default=Sender.OWNER)
    title = models.CharField(max_length=120, blank=True)
    body = models.TextField(blank=True)
    severity = models.CharField(
        max_length=16, choices=Severity.choices, default=Severity.INFO
    )
    # Delivery/seen state, per side. Set true for the side that wrote the line.
    read_by_owner = models.BooleanField(default=False)
    read_by_member = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["member", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.title} -> {self.member} ({self.severity})"


class DeviceEvent(models.Model):
    """
    Something the agent saw on a PC (a USB drive plugged in, a rule applied...).

    `client_id` is the uuid the agent generated when it queued the event; it makes
    uploads idempotent, so the agent can safely re-send after a lost response.
    """

    class Severity(models.TextChoices):
        INFO = "info", "Info"
        WARNING = "warning", "Warning"
        CRITICAL = "critical", "Critical"

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="events")
    client_id = models.UUIDField()
    type = models.CharField(max_length=64)
    severity = models.CharField(max_length=16, choices=Severity.choices, default=Severity.INFO)
    occurred_at = models.DateTimeField()
    received_at = models.DateTimeField(auto_now_add=True)
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-occurred_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["device", "client_id"], name="uniq_event_per_device")
        ]
        indexes = [models.Index(fields=["device", "occurred_at"])]

    def __str__(self) -> str:
        return f"{self.type} on {self.device_id} at {self.occurred_at:%Y-%m-%d %H:%M}"


class UsageMetric(models.Model):
    """
    One hour-bucketed usage counter for a device, feeding the owner's charts.

    The agent rolls per-hour totals per (category, key) -- e.g. category
    "web.domain" key "youtube.com", or "transfer.usb" key "" -- and uploads them.
    A completed hour is sent once; the still-accumulating current hour is re-sent
    each round with growing values, so an upload REPLACES the row for its
    (device, bucket_start, category, key), it never adds to it.
    """

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="metrics")
    bucket_start = models.DateTimeField(help_text="Start of the UTC hour this row counts.")
    category = models.CharField(max_length=64)
    key = models.CharField(max_length=255, blank=True)
    count = models.BigIntegerField(default=0)
    bytes = models.BigIntegerField(default=0)
    seconds = models.BigIntegerField(default=0)

    class Meta:
        ordering = ["-bucket_start", "category", "key"]
        constraints = [
            models.UniqueConstraint(
                fields=["device", "bucket_start", "category", "key"],
                name="uniq_metric_per_bucket",
            )
        ]
        indexes = [models.Index(fields=["device", "bucket_start"])]

    def __str__(self) -> str:
        return f"{self.category}/{self.key} @ {self.bucket_start:%Y-%m-%d %H:00} on {self.device_id}"


class WorkLog(models.Model):
    """
    One change in a member's work day, as the owner's Activity page shows it:
    started work, went on a break (with the reason they picked), came back, ended the day.

    Recorded by the heartbeat when the reported work state changes. `at` is when the
    widget says the change happened (`work_changed_at`), else when we heard about it.
    A break shorter than one heartbeat can be missed; the totals on the device stay exact.
    """

    class State(models.TextChoices):
        WORKING = "working", "Started / back to work"
        ON_BREAK = "on_break", "On break"
        ENDED = "ended", "Ended the day"

    owner = models.ForeignKey("z_auth.User", on_delete=models.CASCADE, related_name="work_logs")
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="work_logs")
    member = models.ForeignKey(
        Member, null=True, blank=True, on_delete=models.SET_NULL, related_name="work_logs"
    )
    state = models.CharField(max_length=16, choices=State.choices)
    previous_state = models.CharField(max_length=16, blank=True)
    at = models.DateTimeField(db_index=True)
    reason = models.CharField(max_length=120, blank=True, help_text="Break reason the member picked.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["at", "id"]
        indexes = [models.Index(fields=["owner", "at"])]

    def __str__(self) -> str:
        return f"{self.member or self.device}: {self.state} @ {self.at:%Y-%m-%d %H:%M}"
