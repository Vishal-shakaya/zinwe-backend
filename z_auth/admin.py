from django import forms
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import BaseUserCreationForm, UserChangeForm
from django.core.exceptions import PermissionDenied
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from . import billing
from .models import (
    Company,
    EarlyAccessRegistration,
    ManualPayment,
    OneTimeCode,
    Plan,
    PlanActivation,
    Subscription,
    User,
)
from .portal_views import proof_file_response


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
    list_display = (
        "email", "full_name", "company", "is_staff", "is_active", "email_verified_at", "date_joined",
    )
    list_filter = ("is_staff", "is_superuser", "is_active", ("email_verified_at", admin.EmptyFieldListFilter))
    search_fields = ("email", "full_name", "company")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("full_name", "company", "phone")}),
        (
            "Permissions",
            {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")},
        ),
        ("Dates", {"fields": ("last_login", "date_joined", "email_verified_at")}),
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
    list_display = (
        "code", "name", "price_inr_monthly", "trial_days", "max_devices", "is_recommended",
        "is_active", "sort_order",
    )
    list_filter = ("is_active", "is_recommended")
    ordering = ("sort_order", "code")
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


# --- Owner portal -------------------------------------------------------------


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "industry", "team_size", "city", "state", "gstin", "created_at")
    list_filter = ("industry", "team_size", "state", "created_at")
    search_fields = ("name", "owner__email", "gstin", "phone", "city")
    raw_id_fields = ("owner",)
    readonly_fields = ("created_at", "updated_at")


class ManualPaymentInline(admin.TabularInline):
    model = ManualPayment
    extra = 0
    can_delete = False
    show_change_link = True
    fields = ("method", "amount_inr", "reference", "paid_on", "status", "submitted_at")
    readonly_fields = fields

    def has_add_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return False


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "id", "owner", "company", "plan", "status", "is_trial", "amount_inr", "starts_at", "ends_at",
        "created_at",
    )
    list_filter = ("status", "is_trial", "plan", "billing_cycle", "created_at")
    search_fields = ("owner__email", "company__name")
    raw_id_fields = ("owner", "company", "activation")
    readonly_fields = ("created_at", "updated_at")
    inlines = [ManualPaymentInline]


class RejectPaymentForm(forms.Form):
    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3, "cols": 70}),
        max_length=1000,
        help_text="Sent to the owner by email, e.g. \"We couldn't find a payment with this UTR.\"",
    )


@admin.register(ManualPayment)
class ManualPaymentAdmin(admin.ModelAdmin):
    """
    Payments to check. Select rows, then run **Verify payment** or
    **Reject payment** (asks for a reason on a second page).
    """

    list_display = (
        "id", "owner_email", "plan", "method", "amount_inr", "reference", "paid_on",
        "status", "has_proof", "submitted_at", "reviewed_at",
    )
    list_filter = ("status", "method", "submitted_at")
    search_fields = ("reference", "payer_name", "subscription__owner__email", "subscription__company__name")
    date_hierarchy = "submitted_at"
    list_select_related = ("subscription__owner", "subscription__plan")
    actions = ["verify_payments", "reject_payments"]
    fields = (
        "subscription", "method", "amount_inr", "reference", "paid_on", "payer_name", "proof_link",
        "status", "rejection_reason", "submitted_at", "reviewed_at", "reviewed_by",
    )
    readonly_fields = (
        "subscription", "proof_link", "status", "rejection_reason", "submitted_at",
        "reviewed_at", "reviewed_by",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False  # owners submit payments through the portal

    @admin.display(description="Owner", ordering="subscription__owner__email")
    def owner_email(self, obj: ManualPayment) -> str:
        return obj.subscription.owner.email

    @admin.display(description="Plan")
    def plan(self, obj: ManualPayment) -> str:
        return obj.subscription.plan.code

    @admin.display(boolean=True, description="Proof")
    def has_proof(self, obj: ManualPayment) -> bool:
        return bool(obj.proof)

    @admin.display(description="Proof")
    def proof_link(self, obj: ManualPayment) -> str:
        if not obj.pk or not obj.proof:
            return "No file"
        url = reverse("admin:z_auth_manualpayment_proof", args=[obj.pk])
        return format_html('<a href="{}">Download proof</a>', url)

    # Proof download: staff only, never a public media URL.
    def get_urls(self):  # type: ignore[no-untyped-def]
        custom = [
            path(
                "<int:payment_id>/proof/",
                self.admin_site.admin_view(self.proof_view),
                name="z_auth_manualpayment_proof",
            )
        ]
        return custom + super().get_urls()

    def proof_view(self, request: HttpRequest, payment_id: int) -> HttpResponse:
        if not self.has_view_permission(request):
            raise PermissionDenied
        return proof_file_response(get_object_or_404(ManualPayment, pk=payment_id))

    # Actions
    @admin.action(description="Verify payment (activates the plan for one month)")
    def verify_payments(self, request: HttpRequest, queryset: QuerySet) -> None:
        done = 0
        for payment in queryset.order_by("pk"):
            try:
                billing.verify_payment(payment.pk, request.user)
                done += 1
            except billing.ReviewNotAllowed as exc:
                self.message_user(request, str(exc), messages.WARNING)
        if done:
            self.message_user(
                request, f"{done} payment(s) verified. The owners have been emailed.", messages.SUCCESS
            )

    @admin.action(description="Reject payment (asks for a reason)")
    def reject_payments(self, request: HttpRequest, queryset: QuerySet) -> TemplateResponse | None:
        form = RejectPaymentForm(request.POST if "apply" in request.POST else None)
        if "apply" in request.POST and form.is_valid():
            done = 0
            for payment in queryset.order_by("pk"):
                try:
                    billing.reject_payment(payment.pk, request.user, form.cleaned_data["reason"])
                    done += 1
                except billing.ReviewNotAllowed as exc:
                    self.message_user(request, str(exc), messages.WARNING)
            if done:
                self.message_user(
                    request, f"{done} payment(s) rejected. The owners have been emailed.", messages.SUCCESS
                )
            return None  # back to the list
        context = {
            **self.admin_site.each_context(request),
            "title": "Reject payment",
            "opts": self.model._meta,
            "payments": queryset.select_related("subscription__owner"),
            "form": form,
            "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME,
        }
        return TemplateResponse(request, "admin/z_auth/manualpayment/reject_payments.html", context)


@admin.register(OneTimeCode)
class OneTimeCodeAdmin(admin.ModelAdmin):
    """Read-only support view. Only a hash of each code is stored."""

    list_display = ("email", "purpose", "created_at", "expires_at", "attempts", "consumed_at", "ip")
    list_filter = ("purpose", "created_at")
    search_fields = ("email",)
    exclude = ("code_hash",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return False
