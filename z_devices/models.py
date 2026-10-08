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


class Device(models.Model):
    """One Windows PC running the agent, owned by the business account that enrolled it."""

    class Platform(models.TextChoices):
        WINDOWS = "windows", "Windows"
        LINUX = "linux", "Linux"
        MACOS = "macos", "macOS"

    owner = models.ForeignKey("z_auth.User", on_delete=models.CASCADE, related_name="devices")
    enrollment_key = models.ForeignKey(
        EnrollmentKey, null=True, blank=True, on_delete=models.SET_NULL, related_name="devices"
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

    owner = models.OneToOneField(
        "z_auth.User", on_delete=models.CASCADE, related_name="device_policy"
    )
    version = models.PositiveIntegerField(default=1)
    usb_mode = models.CharField(max_length=16, choices=UsbMode.choices, default=UsbMode.ALLOW)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "policies"

    def __str__(self) -> str:
        return f"Policy v{self.version} ({self.owner})"


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
