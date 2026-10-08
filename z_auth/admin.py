from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import BaseUserCreationForm, UserChangeForm
from django.db.models import QuerySet
from django.http import HttpRequest

from .models import EarlyAccessRegistration, Plan, PlanActivation, User


class ZinweUserCreationForm(BaseUserCreationForm):
    class Meta:
        model = User
        fields = ("email", "full_name")


class ZinweUserChangeForm(UserChangeForm):
    class Meta:
        model = User
        fields = "__all__"
        field_classes: dict = {}


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    add_form = ZinweUserCreationForm
    form = ZinweUserChangeForm
    ordering = ("-date_joined",)
    list_display = ("email", "full_name", "company", "is_staff", "is_active", "date_joined")
    list_filter = ("is_staff", "is_superuser", "is_active")
    search_fields = ("email", "full_name", "company")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("full_name", "company", "phone")}),
        (
            "Permissions",
            {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")},
        ),
        ("Dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "full_name", "password1", "password2"),
            },
        ),
    )


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "price_inr_monthly", "trial_days", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("code", "name")


@admin.register(PlanActivation)
class PlanActivationAdmin(admin.ModelAdmin):
    list_display = ("user", "plan", "starts_at", "ends_at", "is_active", "source")
    list_filter = ("is_active", "plan", "source")
    search_fields = ("user__email",)
    raw_id_fields = ("user",)


@admin.register(EarlyAccessRegistration)
class EarlyAccessRegistrationAdmin(admin.ModelAdmin):
    list_display = ("email", "name", "company", "team_size", "status", "trial_days", "created_at")
    list_filter = ("status", "team_size", "created_at")
    search_fields = ("email", "name", "company", "phone")
    date_hierarchy = "created_at"
    readonly_fields = ("ip", "user_agent", "created_at", "updated_at")
    actions = ["mark_as_invited"]

    @admin.action(description="Mark as invited")
    def mark_as_invited(self, request: HttpRequest, queryset: QuerySet) -> None:
        updated = queryset.update(status=EarlyAccessRegistration.Status.INVITED)
        self.message_user(request, f"{updated} registration(s) marked as invited.", messages.SUCCESS)

    def formfield_for_dbfield(self, db_field, request, **kwargs):  # type: ignore[no-untyped-def]
        # Show the JSON lists in a compact textarea.
        if db_field.name in {"platforms", "concerns"}:
            kwargs["widget"] = forms.Textarea(attrs={"rows": 2, "cols": 60})
        return super().formfield_for_dbfield(db_field, request, **kwargs)
