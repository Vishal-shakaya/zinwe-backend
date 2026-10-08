from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest

from .events import build_title
from .models import Device, DeviceEvent, EnrollmentKey, Policy


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
        "hostname", "owner", "platform", "agent_version", "last_seen_at", "online", "is_revoked",
    )
    list_filter = ("platform", "is_revoked")
    search_fields = ("hostname", "machine_id", "owner__email")
    raw_id_fields = ("owner", "enrollment_key")
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
    list_display = ("owner", "usb_mode", "version", "updated_at")
    list_filter = ("usb_mode",)
    search_fields = ("owner__email",)
    raw_id_fields = ("owner",)
    # Edit rules from the dashboard (PUT /api/devices/policy/) so the version bumps
    # and the change is tracked; admin is read-only for them.
    readonly_fields = ("owner", "usb_mode", "version", "created_at", "updated_at")


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
