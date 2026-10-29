from django.contrib import admin
from django.http import HttpRequest

from .models import Event, OpsEvent


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    """Read-only event log."""

    list_display = ("created_at", "name", "actor_email", "ref_type", "ref_id")
    list_filter = ("name", "created_at")
    search_fields = ("name", "actor_email", "ref_id")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in Event._meta.fields]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Event | None = None) -> bool:
        return False


@admin.register(OpsEvent)
class OpsEventAdmin(admin.ModelAdmin):
    """FR-37 ops log (the admin app's Logs page). Read-only here: rows come from the server scripts."""

    list_display = ("at", "type", "label", "env", "result", "unresolved", "what")
    list_filter = ("type", "result", "env", "unresolved")
    search_fields = ("what", "who", "label")
    date_hierarchy = "at"
    readonly_fields = [f.name for f in OpsEvent._meta.fields]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: OpsEvent | None = None) -> bool:
        return False
