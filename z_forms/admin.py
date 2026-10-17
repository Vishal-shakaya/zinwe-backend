from django.contrib import admin

from .models import FormSubmission


@admin.register(FormSubmission)
class FormSubmissionAdmin(admin.ModelAdmin):
    list_display = ["id", "kind", "name", "email", "company", "status", "created_at"]
    list_filter = ["kind", "status"]
    search_fields = ["name", "email", "company", "phone", "subject", "message"]
    readonly_fields = ["created_at", "handled_at", "ip", "user_agent"]
