from django.contrib import admin

from .models import SupportMessage, SupportTicket


class MessageInline(admin.TabularInline):
    model = SupportMessage
    extra = 0
    fields = ["sender", "author", "body", "created_at", "read_by_owner", "read_by_staff"]
    readonly_fields = ["created_at"]


@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = ["id", "subject", "owner", "category", "status", "updated_at"]
    list_filter = ["status", "category"]
    search_fields = ["subject", "owner__email"]
    inlines = [MessageInline]
