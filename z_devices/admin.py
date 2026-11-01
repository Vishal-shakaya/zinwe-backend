from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest

from .events import build_title
from .models import (
    Device,
    DeviceEvent,
    EnrollmentKey,
    Member,
    MemberTodo,
    Message,
    Policy,
    Task,
    TaskActivity,
    TaskStep,
    UsageMetric,
)


@admin.register(Member)
class MemberAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "member_code", "owner", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "email", "member_code", "owner__email")
    raw_id_fields = ("owner",)
    readonly_fields = ("member_code", "created_at")


@admin.register(EnrollmentKey)
class EnrollmentKeyAdmin(admin.ModelAdmin):
    list_display = ("key", "owner", "is_active", "created_at", "last_used_at", "revoked_at")
    list_filter = ("is_active",)
    search_fields = ("key", "owner__email")
    raw_id_fields = ("owner",)
    readonly_fields = ("created_at", "last_used_at", "revoked_at")


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = (
        "hostname", "owner", "member", "platform", "agent_version",
        "last_seen_at", "online", "is_revoked",
    )
    list_filter = ("platform", "is_revoked")
    search_fields = ("hostname", "machine_id", "owner__email", "member__email")
    raw_id_fields = ("owner", "enrollment_key", "member")
    readonly_fields = ("token_hash", "enrolled_at", "last_seen_at", "last_ip")
    actions = ["revoke"]

    @admin.display(boolean=True)
    def online(self, obj: Device) -> bool:
        return obj.is_online

    @admin.action(description="Revoke (device must re-enroll)")
    def revoke(self, request: HttpRequest, queryset: QuerySet) -> None:
        updated = queryset.update(is_revoked=True)
        self.message_user(request, f"{updated} device(s) revoked.", messages.SUCCESS)


@admin.register(Policy)
class PolicyAdmin(admin.ModelAdmin):
    list_display = ("owner", "usb_mode", "web_mode", "version", "updated_at")
    list_filter = ("usb_mode", "web_mode", "remote_software_blocking_enabled")
    search_fields = ("owner__email",)
    raw_id_fields = ("owner",)
    # Edit rules from the dashboard (PUT /api/devices/policy/) so the version bumps
    # and the change is tracked; admin is read-only for them.
    readonly_fields = (
        "owner", "version", "created_at", "updated_at",
        "usb_mode",
        "web_enabled", "web_mode", "web_categories", "web_custom_domains", "web_allow_domains", "web_upload_warn_bytes",
        "data_transfer_enabled", "data_transfer_usb_warn_bytes",
        "data_transfer_lan_warn_bytes", "data_transfer_email_warn_bytes",
        "data_transfer_include_details",
        "remote_software_enabled", "remote_software_blocking_enabled",
        "remote_software_blocked_tools", "remote_software_extra_tool_executables",
    )


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("created_at", "title", "severity", "member", "owner")
    list_filter = ("severity",)
    search_fields = ("title", "body", "member__name", "member__email", "owner__email")
    raw_id_fields = ("owner", "member")
    date_hierarchy = "created_at"
    list_select_related = ("member", "owner")
    # Messages are sent from the dashboard (POST .../message/) so the send is
    # tracked; admin is a read-only audit view.
    readonly_fields = ("owner", "member", "title", "body", "severity", "created_at")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:  # type: ignore[no-untyped-def]
        return False


class TaskStepInline(admin.TabularInline):
    model = TaskStep
    extra = 0
    readonly_fields = ("title", "done", "position")
    can_delete = False


class TaskActivityInline(admin.TabularInline):
    model = TaskActivity
    extra = 0
    readonly_fields = ("at", "by", "action", "note")
    can_delete = False


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    """FR-20 tasks: a read-only audit view (owners and members change them from the dashboard and the PC)."""

    list_display = ("updated_at", "title", "status", "member", "owner")
    list_filter = ("status", "priority")
    search_fields = ("title", "member__name", "member__email", "owner__email")
    list_select_related = ("member", "owner")
    raw_id_fields = ("owner", "member", "message")
    inlines = [TaskStepInline, TaskActivityInline]
    readonly_fields = (
        "owner", "member", "message", "title", "note", "due_at", "priority", "status", "help_note",
        "status_changed_at", "status_changed_by", "nudged_at", "created_at", "updated_at",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:  # type: ignore[no-untyped-def]
        return False


@admin.register(MemberTodo)
class MemberTodoAdmin(admin.ModelAdmin):
    """A member's own to-dos: a read-only audit view (only their Zinwe window changes them)."""

    list_display = ("updated_at", "title", "status", "deleted_at", "member", "owner")
    list_filter = ("status",)
    search_fields = ("title", "member__name", "member__email", "owner__email")
    list_select_related = ("member", "owner")
    raw_id_fields = ("owner", "member")
    readonly_fields = (
        "owner", "member", "client_id", "title", "due_date", "steps", "status", "done_at", "updated_at",
        "deleted_at", "created_at",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:  # type: ignore[no-untyped-def]
        return False


@admin.register(DeviceEvent)
class DeviceEventAdmin(admin.ModelAdmin):
    list_display = ("occurred_at", "title", "type", "severity", "device", "received_at")
    list_filter = ("severity", "type")
    search_fields = ("device__hostname", "device__owner__email", "type")
    raw_id_fields = ("device",)
    date_hierarchy = "occurred_at"
    list_select_related = ("device",)
    readonly_fields = (
        "device", "client_id", "type", "severity", "occurred_at", "received_at", "data",
    )

    @admin.display(description="Title")
    def title(self, obj: DeviceEvent) -> str:
        return build_title(obj.type, obj.data, obj.device.hostname)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False  # events only come from agents


@admin.register(UsageMetric)
class UsageMetricAdmin(admin.ModelAdmin):
    list_display = ("bucket_start", "category", "key", "count", "bytes", "seconds", "device")
    list_filter = ("category",)
    search_fields = ("device__hostname", "device__owner__email", "category", "key")
    raw_id_fields = ("device",)
    date_hierarchy = "bucket_start"
    list_select_related = ("device",)
    # Metrics only come from agents (POST metrics/upload/); admin is a read-only view.
    readonly_fields = ("device", "bucket_start", "category", "key", "count", "bytes", "seconds")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:  # type: ignore[no-untyped-def]
        return False
