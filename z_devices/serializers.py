from __future__ import annotations

import json
from datetime import timezone as dt_timezone
from typing import Any

from rest_framework import serializers

from .events import EVENT_TYPE_PATTERN, build_title
from .models import Device, DeviceEvent, EnrollmentKey, Policy


class EnrollSerializer(serializers.Serializer):
    enrollment_key = serializers.CharField(max_length=32)
    machine_id = serializers.CharField(max_length=64)
    hostname = serializers.CharField(max_length=255)
    platform = serializers.ChoiceField(choices=Device.Platform.choices, default=Device.Platform.WINDOWS)
    os_version = serializers.CharField(max_length=128, required=False, allow_blank=True, default="")
    agent_version = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")

    def validate_enrollment_key(self, value: str) -> str:
        return value.strip().upper()

    def validate_machine_id(self, value: str) -> str:
        return value.strip().lower()


class HeartbeatSerializer(serializers.Serializer):
    hostname = serializers.CharField(max_length=255, required=False, allow_blank=True)
    os_version = serializers.CharField(max_length=128, required=False, allow_blank=True)
    agent_version = serializers.CharField(max_length=32, required=False, allow_blank=True)


class EnrollmentKeySerializer(serializers.ModelSerializer):
    class Meta:
        model = EnrollmentKey
        fields = ["key", "created_at", "last_used_at"]


class DeviceSerializer(serializers.ModelSerializer):
    is_online = serializers.BooleanField(read_only=True)

    class Meta:
        model = Device
        fields = [
            "id", "hostname", "platform", "os_version", "agent_version",
            "enrolled_at", "last_seen_at", "is_online", "is_revoked",
        ]


# --- Policy -----------------------------------------------------------------


class UsbRuleSerializer(serializers.Serializer):
    mode = serializers.ChoiceField(choices=Policy.UsbMode.choices)


class PolicyUpdateSerializer(serializers.Serializer):
    """PUT /api/devices/policy/ body: `{"usb": {"mode": "allow"|"read_only"|"block"}}`."""

    usb = UsbRuleSerializer()


class DevicePolicySerializer(serializers.ModelSerializer):
    """What the agent enforces: `{"version": n, "usb": {"mode": "..."}}`."""

    usb = serializers.SerializerMethodField()

    class Meta:
        model = Policy
        fields = ["version", "usb"]

    def get_usb(self, obj: Policy) -> dict[str, str]:
        return {"mode": obj.usb_mode}


class PolicySerializer(DevicePolicySerializer):
    """Owner view: the device shape plus `updated_at`."""

    class Meta(DevicePolicySerializer.Meta):
        fields = ["version", "usb", "updated_at"]


# --- Events -----------------------------------------------------------------

MAX_EVENTS_PER_UPLOAD = 100
MAX_EVENT_DATA_BYTES = 4096


class EventInSerializer(serializers.Serializer):
    """One queued agent event. `id` is the agent's uuid, stored as `client_id`."""

    id = serializers.UUIDField()
    type = serializers.RegexField(
        EVENT_TYPE_PATTERN,
        max_length=64,
        error_messages={"invalid": "Use dotted lowercase names like usb.connected."},
    )
    # Naive timestamps are read as UTC (machine clocks), not the server's TIME_ZONE.
    occurred_at = serializers.DateTimeField(default_timezone=dt_timezone.utc)
    severity = serializers.ChoiceField(
        choices=DeviceEvent.Severity.choices, default=DeviceEvent.Severity.INFO
    )
    data = serializers.JSONField(default=dict)

    def validate_data(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise serializers.ValidationError("Must be a JSON object.")
        size = len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        if size > MAX_EVENT_DATA_BYTES:
            raise serializers.ValidationError(
                f"Too large ({size} bytes); keep it under {MAX_EVENT_DATA_BYTES} bytes."
            )
        return value


class EventUploadSerializer(serializers.Serializer):
    events = EventInSerializer(
        many=True, allow_empty=False, max_length=MAX_EVENTS_PER_UPLOAD
    )


class EventFeedQuerySerializer(serializers.Serializer):
    """Query params for GET /api/devices/events/."""

    device = serializers.IntegerField(min_value=1, required=False)
    limit = serializers.IntegerField(min_value=1, max_value=200, default=50)


class EventSerializer(serializers.ModelSerializer):
    hostname = serializers.CharField(source="device.hostname", read_only=True)
    title = serializers.SerializerMethodField()

    class Meta:
        model = DeviceEvent
        fields = [
            "id", "device_id", "hostname", "type", "severity", "title",
            "occurred_at", "received_at", "data",
        ]

    def get_title(self, obj: DeviceEvent) -> str:
        return build_title(obj.type, obj.data, obj.device.hostname)
