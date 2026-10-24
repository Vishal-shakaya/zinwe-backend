from django.contrib import admin
from django.http import HttpRequest

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    """Read-only delivery log."""

    list_display = ("created_at", "channel", "template", "recipient", "status", "sent_at")
    list_filter = ("status", "channel", "template", "created_at")
    search_fields = ("recipient", "subject", "ref_id")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in Notification._meta.fields if f.name != "retry_payload"]
    exclude = ("retry_payload",)  # the copy kept for a retry (HTML, attachments): not for reading here

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Notification | None = None) -> bool:
        return False
