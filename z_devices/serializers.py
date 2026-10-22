from __future__ import annotations

import json
from datetime import timezone as dt_timezone
from typing import Any

from rest_framework import serializers

from .events import EVENT_TYPE_PATTERN, build_title, event_kind
from .models import Device, DeviceEvent, EnrollmentKey, Member, Message, Policy, WorkLog


class EnrollSerializer(serializers.Serializer):
    """The per-member enrol payload the agent posts: email + member id, no org key."""

    email = serializers.EmailField(max_length=254)
    member_id = serializers.CharField(max_length=16)
    machine_id = serializers.CharField(max_length=64)
    hostname = serializers.CharField(max_length=255)
    platform = serializers.ChoiceField(choices=Device.Platform.choices, default=Device.Platform.WINDOWS)
    os_version = serializers.CharField(max_length=128, required=False, allow_blank=True, default="")
    agent_version = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")

    def validate_member_id(self, value: str) -> str:
        return value.strip().upper()

    def validate_email(self, value: str) -> str:
        return value.strip().lower()

    def validate_machine_id(self, value: str) -> str:
        return value.strip().lower()


class MemberCreateSerializer(serializers.Serializer):
    """POST /api/devices/members/ -- the owner registers one employee."""

    name = serializers.CharField(max_length=150)
    email = serializers.EmailField(max_length=254)


class MemberSerializer(serializers.Serializer):
    """
    Owner view of a member: identity, the enrol id, and its current PC (if any)
    with online status. `device` is null until that member's agent has enrolled.
    """

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    email = serializers.EmailField(read_only=True)
    member_id = serializers.CharField(source="member_code", read_only=True)
    is_active = serializers.BooleanField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    online = serializers.SerializerMethodField()
    device = serializers.SerializerMethodField()
    # Where this member downloads the agent — same link the invite email carries,
    # so the owner's "copy details / share" button can hand over everything at once.
    download_url = serializers.SerializerMethodField()
    # Unread replies from this member (for the Team list chat badge).
    unread = serializers.SerializerMethodField()

    def get_download_url(self, obj: Member) -> str:
        from django.conf import settings

        return settings.ZINWE_AGENT_DOWNLOAD_URL

    def get_unread(self, obj: Member) -> int:
        from . import services

        return services.unread_from_member(obj)

    def _current_device(self, obj: Member):  # type: ignore[no-untyped-def]
        from . import services

        return services.current_device_for(obj)

    def get_online(self, obj: Member) -> bool:
        device = self._current_device(obj)
        return bool(device and device.is_online)

    def get_device(self, obj: Member) -> dict[str, Any] | None:
        device = self._current_device(obj)
        if device is None:
            return None
        return {
            "id": device.id,
            "hostname": device.hostname,
            "last_seen_at": device.last_seen_at,
            "is_online": device.is_online,
            "agent_version": device.agent_version,
            "work_state": device.work_state,
            # Today's totals including the stretch still running (FR-03).
            "worked_seconds": device.live_work_seconds()[0],
            "break_seconds": device.live_work_seconds()[1],
            "consent_version": device.consent_version,
            "consent_at": device.consent_at,
            "consent_state": device.consent_state,
            "consent_declined_at": device.consent_declined_at,
            "usb_locked_at": device.usb_locked_at,
            "usb_restore_requested_at": device.usb_restore_requested_at,
        }


class MemberRotateSerializer(serializers.Serializer):
    """POST /api/devices/members/<id>/rotate/ -- {email: true} re-sends the invite with the new ID."""

    email = serializers.BooleanField(required=False, default=True)


class MessageCreateSerializer(serializers.Serializer):
    """POST /api/devices/members/<id>/message/ -- the owner sends one note."""

    title = serializers.CharField(max_length=120, trim_whitespace=True)
    body = serializers.CharField(
        max_length=2000, required=False, allow_blank=True, default="", trim_whitespace=False
    )
    severity = serializers.ChoiceField(
        choices=Message.Severity.choices, default=Message.Severity.INFO
    )
    # Where it goes: the widget, widget + email, or email only. Older clients send `also_email`
    # instead (true = chat_email); `channel` wins when both are given.
    channel = serializers.ChoiceField(choices=Message.Channel.choices, required=False)
    also_email = serializers.BooleanField(required=False, default=False)
    # Sent from an alert: that alert is marked handled (J5-04).
    event_id = serializers.IntegerField(min_value=1, required=False, allow_null=True, default=None)

    def validate(self, attrs: dict) -> dict:
        if not attrs.get("channel"):
            attrs["channel"] = Message.Channel.CHAT_EMAIL if attrs["also_email"] else Message.Channel.CHAT
        return attrs


class EventEmailSerializer(serializers.Serializer):
    """POST /api/devices/events/<id>/email/ -- the dashboard's wording for the alert email."""

    label = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    suggested = serializers.CharField(max_length=300, required=False, allow_blank=True, default="")


class MessageSerializer(serializers.ModelSerializer):
    """
    The wire shape the agent reads (and the owner's POST echoes).

    `body` is null, never "", when the owner omitted it -- the agent/widget keys
    off null to hide the second line.
    """

    body = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = [
            "id", "sender", "channel", "title", "body", "severity", "created_at", "read_by_owner", "read_by_member",
            "delivered_at", "read_by_member_at",
        ]

    def get_body(self, obj: Message) -> str | None:
        return obj.body or None


class EventHandledSerializer(serializers.Serializer):
    """POST /api/devices/events/<id>/handled/ -- {handled: true|false}."""

    handled = serializers.BooleanField(required=False, default=True)


class MemberReplySerializer(serializers.Serializer):
    """POST /api/devices/messages/ (Device auth) -- the member replies in the thread."""

    body = serializers.CharField(max_length=2000, trim_whitespace=True)


MAX_WORK_EVENTS = 200


class WorkEventSerializer(serializers.Serializer):
    """One change the member made in their widget (log in, break, back, log out), as it happened."""

    at = serializers.DateTimeField()
    state = serializers.ChoiceField(
        choices=[Device.WorkState.WORKING, Device.WorkState.ON_BREAK, Device.WorkState.ENDED]
    )
    # The break reason/note for a break, the log-out reason/note for a log out; empty otherwise.
    reason = serializers.CharField(max_length=120, required=False, allow_blank=True, allow_null=True, default="")
    note = serializers.CharField(max_length=280, required=False, allow_blank=True, allow_null=True, default="")


class ConsentSerializer(serializers.Serializer):
    """`consent: {"state": "given"|"declined", "version": "<notice version>"}` on the heartbeat."""

    state = serializers.ChoiceField(choices=[("given", "Given"), ("declined", "Declined")])
    version = serializers.CharField(max_length=20, required=False, allow_blank=True, allow_null=True, default="")


class HeartbeatSerializer(serializers.Serializer):
    hostname = serializers.CharField(max_length=255, required=False, allow_blank=True)
    os_version = serializers.CharField(max_length=128, required=False, allow_blank=True)
    agent_version = serializers.CharField(max_length=32, required=False, allow_blank=True)
    # Work status (M6). All optional with safe defaults so an older agent that
    # omits them still heartbeats fine.
    work_state = serializers.ChoiceField(
        choices=Device.WorkState.choices,
        required=False,
        default=Device.WorkState.NOT_STARTED,
    )
    worked_seconds = serializers.IntegerField(min_value=0, required=False, default=0)
    break_seconds = serializers.IntegerField(min_value=0, required=False, default=0)
    # When the widget's current state began, and the reason picked for a break.
    # Optional (older agents): we fall back to "now" and no reason.
    work_changed_at = serializers.DateTimeField(required=False, allow_null=True, default=None)
    # The member's consent on the widget (which policy version, and when). Optional: older agents omit it.
    consent_version = serializers.CharField(max_length=20, required=False, allow_blank=True, allow_null=True, default="")
    consent_at = serializers.DateTimeField(required=False, allow_null=True, default=None)
    # Newer agents: the member's answer, which may be "declined" (then nothing is collected from this PC).
    consent = ConsentSerializer(required=False, allow_null=True, default=None)
    break_reason = serializers.CharField(
        max_length=120, required=False, allow_blank=True, allow_null=True, default=""
    )
    break_note = serializers.CharField(
        max_length=280, required=False, allow_blank=True, allow_null=True, default=""
    )
    # Why the member logged out (picked in the widget) and their note. Optional (older agents).
    end_reason = serializers.CharField(
        max_length=120, required=False, allow_blank=True, allow_null=True, default=""
    )
    end_note = serializers.CharField(
        max_length=280, required=False, allow_blank=True, allow_null=True, default=""
    )
    # Every log in / break / back / log out the widget recorded since the last heartbeat, with its exact
    # time (newer agents). When present it replaces the "state changed between heartbeats" guess, so short
    # changes and ones made while offline are not lost. Absent/null = older agent.
    work_events = WorkEventSerializer(many=True, required=False, allow_null=True)

    def validate_work_events(self, value: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if value is not None and len(value) > MAX_WORK_EVENTS:
            raise serializers.ValidationError(f"Send at most {MAX_WORK_EVENTS} work events per heartbeat.")
        return value


class WorkLogQuerySerializer(serializers.Serializer):
    """GET /api/devices/work-log/?date=YYYY-MM-DD&member=<pk>."""

    date = serializers.DateField(required=False)
    member = serializers.IntegerField(min_value=1, required=False)


class BreakDecisionSerializer(serializers.Serializer):
    """POST /api/devices/work-log/<id>/decision/ -- approve or deny one break."""

    decision = serializers.ChoiceField(choices=[("approved", "Approve"), ("denied", "Deny")])
    note = serializers.CharField(max_length=280, required=False, allow_blank=True, default="")


class WorkLogSerializer(serializers.ModelSerializer):
    hostname = serializers.CharField(source="device.hostname", read_only=True)
    member_name = serializers.SerializerMethodField()

    class Meta:
        model = WorkLog
        fields = [
            "id", "device_id", "hostname", "member_id", "member_name", "state", "previous_state", "at",
            "reason", "note", "approval", "decided_at", "decision_note",
        ]

    def get_member_name(self, obj: WorkLog) -> str | None:
        return obj.member.name if obj.member_id else None


class EnrollmentKeySerializer(serializers.ModelSerializer):
    class Meta:
        model = EnrollmentKey
        fields = ["key", "created_at", "last_used_at"]


class DeviceSerializer(serializers.ModelSerializer):
    is_online = serializers.BooleanField(read_only=True)
    # The member's pk (Member.id), so the dashboard can pair each PC with its person.
    member_id = serializers.IntegerField(read_only=True, allow_null=True)
    member_name = serializers.SerializerMethodField()
    consent_state = serializers.CharField(read_only=True)
    # Today's totals including the stretch still running (FR-03).
    worked_seconds = serializers.SerializerMethodField()
    break_seconds = serializers.SerializerMethodField()

    class Meta:
        model = Device
        fields = [
            "id", "hostname", "platform", "os_version", "agent_version",
            "enrolled_at", "last_seen_at", "is_online", "is_revoked", "member_id", "member_name",
            "work_state", "worked_seconds", "break_seconds", "work_updated_at",
            # USB read-only lockdown + the owner's "Restore USB" (J5-02); the member's consent answer (J6-03).
            "usb_locked_at", "usb_restore_requested_at", "consent_state", "consent_declined_at",
        ]

    def get_member_name(self, obj: Device) -> str | None:
        return obj.member.name if obj.member_id else None

    def get_worked_seconds(self, obj: Device) -> int:
        return obj.live_work_seconds()[0]

    def get_break_seconds(self, obj: Device) -> int:
        return obj.live_work_seconds()[1]


# --- Policy -----------------------------------------------------------------


class _StrictCharField(serializers.CharField):
    """Like CharField but refuses non-strings (plain CharField would stringify 42 -> "42")."""

    def to_internal_value(self, data: Any) -> str:
        if not isinstance(data, str):
            self.fail("invalid")
        return super().to_internal_value(data)

    default_error_messages = {"invalid": "Must be a string."}


class _StringListField(serializers.ListField):
    """A list where every item must be a (trimmed, non-empty) string."""

    child = _StrictCharField(max_length=255, allow_blank=False, trim_whitespace=True)


class UsbRuleSerializer(serializers.Serializer):
    mode = serializers.ChoiceField(choices=Policy.UsbMode.choices)


class WebRuleSerializer(serializers.Serializer):
    """`{"enabled", "mode", "categories": [...], "custom_domains": [...]}`."""

    enabled = serializers.BooleanField()
    mode = serializers.ChoiceField(choices=Policy.WebMode.choices)
    # Any string is accepted; the dashboard offers the known vocabulary, but an
    # unknown category is harmless (it just won't match until real lists ship).
    categories = _StringListField()
    custom_domains = _StringListField()


class DataTransferRuleSerializer(serializers.Serializer):
    """Sizes/counts always; filenames only when `include_details` is on."""

    enabled = serializers.BooleanField()
    usb_warn_bytes = serializers.IntegerField(min_value=0)
    lan_warn_bytes = serializers.IntegerField(min_value=0)
    email_warn_bytes = serializers.IntegerField(min_value=0)
    include_details = serializers.BooleanField()
    # Optional for back-compat: an older dashboard omits it → lockdown stays off.
    usb_lockdown = serializers.BooleanField(required=False, default=False)


class RemoteSoftwareRuleSerializer(serializers.Serializer):
    """Always detects; `blocking_enabled` opts into terminating the listed tools."""

    enabled = serializers.BooleanField()
    blocking_enabled = serializers.BooleanField()
    blocked_tools = _StringListField()
    extra_tool_executables = _StringListField()


class PolicyUpdateSerializer(serializers.Serializer):
    """
    PUT /api/devices/policy/ -- change one or more feature rules at once.

    Every feature block is optional so the dashboard can save a single feature
    (e.g. `{"usb": {"mode": "block"}}`) without resending the others. At least
    one known block must be present. New features add their block here and a
    matching branch in `services.update_policy`.
    """

    usb = UsbRuleSerializer(required=False)
    web = WebRuleSerializer(required=False)
    data_transfer = DataTransferRuleSerializer(required=False)
    remote_software = RemoteSoftwareRuleSerializer(required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if not attrs:
            raise serializers.ValidationError("Include at least one rule to change.")
        return attrs


class DevicePolicySerializer(serializers.ModelSerializer):
    """
    What the agent enforces: all four feature blocks in their wire shape plus
    `version`. The agent tolerates an omitted block, but we always send them so
    the dashboard round-trips real state.
    """

    usb = serializers.SerializerMethodField()
    web = serializers.SerializerMethodField()
    data_transfer = serializers.SerializerMethodField()
    remote_software = serializers.SerializerMethodField()

    class Meta:
        model = Policy
        fields = ["version", "usb", "web", "data_transfer", "remote_software"]

    def get_usb(self, obj: Policy) -> dict[str, str]:
        return {"mode": obj.usb_mode}

    def get_web(self, obj: Policy) -> dict[str, Any]:
        return {
            "enabled": obj.web_enabled,
            "mode": obj.web_mode,
            "categories": obj.web_categories,
            "custom_domains": obj.web_custom_domains,
        }

    def get_data_transfer(self, obj: Policy) -> dict[str, Any]:
        return {
            "enabled": obj.data_transfer_enabled,
            "usb_warn_bytes": obj.data_transfer_usb_warn_bytes,
            "lan_warn_bytes": obj.data_transfer_lan_warn_bytes,
            "email_warn_bytes": obj.data_transfer_email_warn_bytes,
            "include_details": obj.data_transfer_include_details,
            "usb_lockdown": obj.data_transfer_usb_lockdown,
        }

    def get_remote_software(self, obj: Policy) -> dict[str, Any]:
        return {
            "enabled": obj.remote_software_enabled,
            "blocking_enabled": obj.remote_software_blocking_enabled,
            "blocked_tools": obj.remote_software_blocked_tools,
            "extra_tool_executables": obj.remote_software_extra_tool_executables,
        }


class PolicySerializer(DevicePolicySerializer):
    """Owner view: the device shape plus `updated_at`."""

    class Meta(DevicePolicySerializer.Meta):
        fields = [*DevicePolicySerializer.Meta.fields, "updated_at"]


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
    # One team member's PCs (the dashboard's "Everyone / one person" filter).
    member = serializers.IntegerField(min_value=1, required=False)
    # Comma-separated event types, e.g. "web.blocked_attempt" -- a feature page asks only for its own,
    # so busy types elsewhere can't push its alerts out of the `limit`.
    types = serializers.CharField(max_length=1000, required=False)
    limit = serializers.IntegerField(min_value=1, max_value=200, default=50)
    # "alert" = rule confirmations left out; "confirmation" = only them. Omitted = everything.
    kind = serializers.ChoiceField(choices=[("alert", "Alerts"), ("confirmation", "Confirmations")], required=False)
    handled = serializers.BooleanField(required=False, allow_null=True, default=None)

    def validate_types(self, value: str) -> list[str]:
        types = [t.strip() for t in value.split(",") if t.strip()]
        if len(types) > 40 or any(len(t) > 64 for t in types):
            raise serializers.ValidationError("Too many or too long event types.")
        return types


class EventSerializer(serializers.ModelSerializer):
    hostname = serializers.CharField(source="device.hostname", read_only=True)
    title = serializers.SerializerMethodField()
    kind = serializers.SerializerMethodField()

    class Meta:
        model = DeviceEvent
        fields = [
            "id", "device_id", "hostname", "type", "severity", "title",
            "occurred_at", "received_at", "data", "kind", "handled_at", "handled_by",
        ]

    def get_title(self, obj: DeviceEvent) -> str:
        return build_title(obj.type, obj.data, obj.device.hostname)

    def get_kind(self, obj: DeviceEvent) -> str:
        """ "confirmation" for rule confirmations (kept out of alert counts), "alert" for everything else."""
        return event_kind(obj.type)


# --- Usage metrics ----------------------------------------------------------

MAX_METRICS_PER_UPLOAD = 1000


class MetricInSerializer(serializers.Serializer):
    """
    One hour-bucketed usage row from the agent.

    `key` may be empty (e.g. a bare "transfer.usb" total); `category` may not.
    Counts are non-negative. `bucket_start` naive timestamps are read as UTC.
    """

    bucket_start = serializers.DateTimeField(default_timezone=dt_timezone.utc)
    category = serializers.CharField(max_length=64, allow_blank=False, trim_whitespace=True)
    key = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default="", trim_whitespace=True
    )
    count = serializers.IntegerField(min_value=0, required=False, default=0)
    bytes = serializers.IntegerField(min_value=0, required=False, default=0)
    seconds = serializers.IntegerField(min_value=0, required=False, default=0)


class MetricUploadSerializer(serializers.Serializer):
    """POST /api/devices/metrics/upload/ -- 1..1000 hour-bucketed rows."""

    metrics = MetricInSerializer(
        many=True, allow_empty=False, max_length=MAX_METRICS_PER_UPLOAD
    )


class MetricsQuerySerializer(serializers.Serializer):
    """Query params for the owner's GET /api/devices/metrics/."""

    member = serializers.IntegerField(min_value=1, required=False)
    device = serializers.IntegerField(min_value=1, required=False)
    days = serializers.IntegerField(min_value=1, max_value=31, default=7)
    category = serializers.CharField(max_length=64, required=False)


class MetricsTopQuerySerializer(serializers.Serializer):
    """Query params for the owner's GET /api/devices/metrics/top/ (per-key breakdown)."""

    category = serializers.CharField(max_length=64)
    member = serializers.IntegerField(min_value=1, required=False)
    device = serializers.IntegerField(min_value=1, required=False)
    days = serializers.IntegerField(min_value=1, max_value=31, default=7)
    limit = serializers.IntegerField(min_value=1, max_value=100, default=20)
