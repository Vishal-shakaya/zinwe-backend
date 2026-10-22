"""Shapes for the internal admin API. Output serializers read services' views / models."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.utils import timezone
from rest_framework import serializers

from z_auth import billing
from z_auth.models import Announcement, Feedback, Invoice, ManualPayment, Plan, Subscription
from z_devices.models import AgentRelease
from z_devices.services import release_adoption
from z_notification.models import Notification

from . import services


class PlanSummarySerializer(serializers.Serializer):
    code = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)
    price_inr_monthly = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    max_devices = serializers.IntegerField(read_only=True)


def payment_dict(pay: ManualPayment, cycle: int | None) -> dict[str, Any]:
    """A reported payment, as the admin app shows it (proof is fetched separately, staff-only)."""
    return {
        "id": pay.id,
        "method": pay.method,
        "amount_inr": str(pay.amount_inr),
        "reference": pay.reference,
        "payer_name": pay.payer_name,
        "paid_on": pay.paid_on,
        "status": pay.status,
        "has_proof": bool(pay.proof),
        "rejection_reason": pay.rejection_reason,
        "submitted_at": pay.submitted_at,
        "reviewed_at": pay.reviewed_at,
        "reviewed_by": pay.reviewed_by.email if pay.reviewed_by_id else None,
        # An upgrade payment is for the plan it moves to (and that plan's price).
        "plan": {
            "code": (pay.upgrade_plan or pay.subscription.plan).code,
            "name": (pay.upgrade_plan or pay.subscription.plan).name,
        },
        "expected_inr": str(pay.upgrade_plan.price_inr_monthly if pay.upgrade_plan_id else pay.subscription.amount_inr),
        "upgrade_from": pay.subscription.plan.name if pay.upgrade_plan_id else None,
        "cycle": cycle,
        "is_renewal": pay.is_renewal,
        "terms_accepted_at": pay.terms_accepted_at,
        "terms_version": pay.terms_version,
        # The period it paid for (approved), or would pay for if approved now (waiting).
        "covers_from": pay.covers_from,
        "covers_until": pay.covers_until,
        "would_cover": _would_cover(pay),
        # What was due when they paid, with tax (the quote frozen on the payment): compare the receipt with this.
        "currency": pay.currency,
        "due_total": (pay.due or {}).get("total") or None,
        # What staff read off the receipt when approving (J8-01), and who did.
        "staff_reference": pay.staff_reference,
        "staff_amount": str(pay.staff_amount) if pay.staff_amount is not None else None,
        "staff_checked_by": pay.staff_checked_by.email if pay.staff_checked_by_id else None,
        # Same receipt file or same UTR as another payment (J8-03).
        "duplicate_of": services.duplicate_of(pay),
        "invoice_number": _invoice_number(pay),
    }


def _invoice_number(pay: ManualPayment) -> str | None:
    if pay.status != ManualPayment.Status.VERIFIED:
        return None
    try:
        return pay.invoice.number
    except Invoice.DoesNotExist:
        return None


def _would_cover(pay: ManualPayment) -> dict[str, Any] | None:
    if pay.status != ManualPayment.Status.SUBMITTED:
        return None
    start, end = billing.next_period(pay)
    return {"from": start, "until": end}


class SubscriberSerializer(serializers.Serializer):
    """Serializes a services.SubscriberView (user + company + subscription + payments)."""

    id = serializers.IntegerField(source="user.id", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)
    full_name = serializers.CharField(source="user.full_name", read_only=True)
    is_active = serializers.BooleanField(source="user.is_active", read_only=True)
    email_verified = serializers.BooleanField(source="user.email_verified", read_only=True)
    date_joined = serializers.DateTimeField(source="user.date_joined", read_only=True)
    last_login = serializers.DateTimeField(source="user.last_login", read_only=True)

    company = serializers.SerializerMethodField()
    plan = serializers.SerializerMethodField()
    subscription = serializers.SerializerMethodField()
    payment = serializers.SerializerMethodField()
    billing = serializers.SerializerMethodField()
    payments = serializers.SerializerMethodField()
    remark = serializers.SerializerMethodField()
    emails = serializers.SerializerMethodField()
    team = serializers.SerializerMethodField()
    terms = serializers.SerializerMethodField()
    blocked = serializers.SerializerMethodField()

    def get_terms(self, obj: Any) -> dict[str, Any]:
        """When the customer accepted the Terms and Privacy Policy at sign-up, and which version."""
        return {"accepted_at": obj.user.terms_accepted_at, "version": obj.user.terms_version}

    def get_company(self, obj: Any) -> dict[str, Any] | None:
        if obj.company is None:
            return None
        return {
            "name": obj.company.name,
            "industry": obj.company.industry,
            "team_size": obj.company.team_size,
            "city": obj.company.city,
            "state": obj.company.state,
        }

    def get_plan(self, obj: Any) -> dict[str, Any] | None:
        if obj.subscription is None:
            return None
        plan = obj.subscription.plan
        return {"code": plan.code, "name": plan.name, "max_devices": plan.max_devices}

    def get_subscription(self, obj: Any) -> dict[str, Any] | None:
        sub = obj.subscription
        if sub is None:
            return None
        return {
            "status": sub.status,
            "is_trial": sub.is_trial,
            "amount_inr": str(sub.amount_inr),
            "starts_at": sub.starts_at,
            "ends_at": sub.ends_at,
            "days_left": billing.days_left(sub),
            "renewable": billing.is_paid_active(sub),
        }

    def get_payment(self, obj: Any) -> dict[str, Any] | None:
        """The current subscription's latest payment (what needs a decision, if anything)."""
        if obj.payment is None:
            return None
        return payment_dict(obj.payment, obj.cycles.get(obj.payment.id))

    def get_billing(self, obj: Any) -> dict[str, Any]:
        """Paid cycles so far (each approved payment = one cycle) and the total received."""
        return {"cycles_paid": obj.cycles_paid, "total_paid_inr": str(obj.total_paid_inr)}

    def get_payments(self, obj: Any) -> list[dict[str, Any]]:
        """Every payment the customer reported, newest first, each with its cycle number."""
        return [payment_dict(p, obj.cycles.get(p.id)) for p in obj.history]

    def get_team(self, obj: Any) -> dict[str, Any]:
        """The members the owner added on their dashboard (name, email, PCs) and enrolled PCs."""
        return {
            "devices": obj.device_count,
            "members": [
                {
                    "id": m.id,
                    "name": m.name,
                    "email": m.email,
                    "member_code": m.member_code,
                    "is_active": m.is_active,
                    "devices": getattr(m, "device_count", 0),
                    "created_at": m.created_at,
                    # Their consent on the Zinwe widget (latest PC that reported one).
                    "consent": _member_consent(m),
                }
                for m in obj.members
            ],
        }

    def get_emails(self, obj: Any) -> dict[str, Any]:
        """Custom emails the team sent this customer (how many, when the last went), and every email Zinwe sent them."""
        return {"sent": obj.emails_sent, "last_sent_at": obj.last_email_at, "total": obj.emails_total}

    def get_blocked(self, obj: Any) -> dict[str, Any] | None:
        """While the account is blocked: who blocked it, when, why, and whether the customer was emailed."""
        event = obj.blocked
        if event is None:
            return None
        props = event.properties or {}
        return {
            "at": event.created_at,
            "by": event.actor_email or None,
            "reason": props.get("reason", ""),
            "emailed": bool(props.get("emailed")),
        }

    def get_remark(self, obj: Any) -> dict[str, Any] | None:
        """The team's internal note on this customer (never shown to them)."""
        r = obj.remark
        if r is None:
            return None
        return {
            "text": r.text,
            "updated_at": r.updated_at,
            "updated_by": r.updated_by.email if r.updated_by_id else None,
        }


def _member_consent(member: Any) -> dict[str, Any] | None:
    device = (
        member.devices.filter(is_revoked=False).exclude(consent_version="").order_by("-consent_at").first()
        if hasattr(member, "devices") else None
    )
    return {"version": device.consent_version, "at": device.consent_at} if device else None


class PaymentQueueSerializer(serializers.Serializer):
    """Serializes a services.PaymentView: the payment plus who it's from."""

    def to_representation(self, instance: Any) -> dict[str, Any]:
        pay = instance.payment
        owner = pay.subscription.owner
        company = getattr(owner, "owned_company", None)
        return {
            **payment_dict(pay, instance.cycle),
            "customer": {
                "id": owner.id,
                "email": owner.email,
                "full_name": owner.full_name,
                "is_active": owner.is_active,
                "company": company.name if company else None,
            },
            "subscription_status": pay.subscription.status,
        }


class FeedbackRowSerializer(serializers.ModelSerializer):
    customer = serializers.SerializerMethodField()
    member = serializers.SerializerMethodField()

    class Meta:
        model = Feedback
        fields = ["id", "rating", "message", "page", "created_at", "customer", "source", "member"]

    def get_member(self, obj: Feedback) -> dict[str, Any] | None:
        """Who sent it from their Zinwe window (FR-14); None for the owner's own feedback."""
        if obj.source != Feedback.Source.MEMBER:
            return None
        return {
            "name": obj.member.name if obj.member else "",
            "hostname": obj.device.hostname if obj.device else "",
        }

    def get_customer(self, obj: Feedback) -> dict[str, Any]:
        company = getattr(obj.owner, "owned_company", None)
        return {
            "id": obj.owner_id,
            "email": obj.owner.email,
            "full_name": obj.owner.full_name,
            "company": company.name if company else None,
        }


class RejectPaymentSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500)


class ApprovePaymentSerializer(serializers.Serializer):
    """What staff read off the receipt before approving (J8-01): both are required."""

    reference = serializers.CharField(max_length=64, error_messages={
        "required": "Type the UTR / reference you see on the receipt.",
        "blank": "Type the UTR / reference you see on the receipt.",
    })
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"), error_messages={
        "required": "Type the amount you see on the receipt.",
        "invalid": "Type the amount as a number, e.g. 971 or 1145.78.",
        "min_value": "Type the amount you see on the receipt.",
    })


class BlockSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500, error_messages={
        "required": "Say why you're blocking this account.",
        "blank": "Say why you're blocking this account.",
    })
    email_customer = serializers.BooleanField(default=True)


class ChangePlanSerializer(serializers.Serializer):
    plan = serializers.SlugField()
    email_customer = serializers.BooleanField(default=False)


class SetPasswordSerializer(serializers.Serializer):
    password = serializers.CharField(max_length=128, trim_whitespace=False)
    email_customer = serializers.BooleanField(default=True)


class RemarkSerializer(serializers.Serializer):
    text = serializers.CharField(max_length=2000, allow_blank=True)


class SendEmailSerializer(serializers.Serializer):
    subject = serializers.CharField(max_length=200)
    html = serializers.CharField(max_length=50_000)


class SentEmailSerializer(serializers.ModelSerializer):
    """An email to the customer (the logged Notification: plain-text body, status).
    `automatic`: sent by Zinwe itself (e.g. a renewal reminder), not written by the team."""

    automatic = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = ["id", "subject", "body", "status", "error", "created_at", "sent_at", "template", "automatic"]

    def get_automatic(self, obj: Notification) -> bool:
        return obj.template != "admin_message"


# --- Agent releases (Releases page) ------------------------------------------------


class ReleaseRowSerializer(serializers.ModelSerializer):
    """One published Windows agent release, with how many PCs already run it."""

    hosted = serializers.SerializerMethodField()
    published_by = serializers.SerializerMethodField()
    adoption = serializers.SerializerMethodField()

    class Meta:
        model = AgentRelease
        fields = [
            "id", "version", "notes", "required", "is_active", "hosted", "download_url",
            "sha256", "size", "published_at", "published_by", "adoption",
        ]

    def get_hosted(self, obj: Any) -> str:
        return "upload" if obj.file else "link"

    def get_published_by(self, obj: Any) -> str:
        return obj.published_by.email if obj.published_by else ""

    def get_adoption(self, obj: Any) -> dict[str, int]:
        return release_adoption(obj)


class PublishReleaseSerializer(serializers.Serializer):
    """POST /api/admin/releases/ (multipart): the MSI upload, or an external link + its SHA-256."""

    version = serializers.CharField(max_length=32)
    notes = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")
    # Optional unless staff tick it (J8-06): a required update pauses log in / breaks / log out on older PCs.
    required = serializers.BooleanField(required=False, default=False)
    file = serializers.FileField(required=False, allow_null=True, max_length=200)
    download_url = serializers.URLField(required=False, allow_blank=True, default="")
    sha256 = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")

    def validate_file(self, value: Any) -> Any:
        if value is not None and value.size > MAX_RELEASE_BYTES:
            raise serializers.ValidationError("The installer is larger than 300 MB.")
        return value


MAX_RELEASE_BYTES = 300 * 1024 * 1024


class UpdateReleaseSerializer(serializers.Serializer):
    """PATCH /api/admin/releases/<id>/: make required/optional, withdraw/restore, edit notes."""

    required = serializers.BooleanField(required=False)
    is_active = serializers.BooleanField(required=False)
    notes = serializers.CharField(max_length=2000, required=False, allow_blank=True)


# --- Announcements (notification bars on the dashboard and website) ---------------------------


class AnnouncementSerializer(serializers.ModelSerializer):
    """GET/POST /api/admin/announcements/ and PATCH /<id>/: one pushed message plus its live status."""

    status = serializers.SerializerMethodField()
    created_by = serializers.SerializerMethodField()

    class Meta:
        model = Announcement
        fields = (
            "id", "title", "body", "kind", "placement", "audience", "plan_codes", "cta_label", "cta_href",
            "starts_at", "ends_at", "is_active", "dismissible", "priority", "status", "created_by",
            "created_at", "updated_at",
        )
        read_only_fields = ("id", "status", "created_by", "created_at", "updated_at")

    def get_status(self, obj: Announcement) -> str:
        """live · scheduled · ended · off — what owners see right now."""
        now = timezone.now()
        if not obj.is_active:
            return "off"
        if obj.starts_at > now:
            return "scheduled"
        if obj.ends_at is not None and obj.ends_at <= now:
            return "ended"
        return "live"

    def get_created_by(self, obj: Announcement) -> str:
        return obj.created_by.email if obj.created_by else ""

    def validate_title(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("Write a short headline.")
        return value.strip()

    def validate_cta_href(self, value: str) -> str:
        value = value.strip()
        is_path = value.startswith("/") and not value.startswith("//")
        is_mail = value.lower().startswith("mailto:") and "@" in value
        if value and not (is_path or is_mail or value.startswith("https://")):
            raise serializers.ValidationError(
                "Use a page on zinwe.io (starting with /), an https:// link, or mailto:hello@zinwe.io."
            )
        return value

    def validate_plan_codes(self, value: Any) -> list[str]:
        if not isinstance(value, list) or not all(isinstance(code, str) for code in value):
            raise serializers.ValidationError("A list of plan codes.")
        known = set(Plan.objects.values_list("code", flat=True))
        unknown = [code for code in value if code not in known]
        if unknown:
            raise serializers.ValidationError(f"Unknown plan: {', '.join(unknown)}.")
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        # A PATCH sends only what changed: check the rules against the saved values plus the change.
        saved = {} if self.instance is None else {
            name: getattr(self.instance, name)
            for name in ("cta_label", "cta_href", "starts_at", "ends_at", "audience", "plan_codes")
        }
        merged = {**saved, **attrs}
        if bool(merged.get("cta_label", "").strip()) != bool(merged.get("cta_href", "").strip()):
            raise serializers.ValidationError({"cta_href": "Give the button both a text and a link, or neither."})
        starts, ends = merged.get("starts_at"), merged.get("ends_at")
        if starts and ends and ends <= starts:
            raise serializers.ValidationError({"ends_at": "The end must be after the start."})
        if merged.get("audience") == Announcement.Audience.PLANS and not merged.get("plan_codes"):
            raise serializers.ValidationError({"plan_codes": "Pick at least one plan."})
        return attrs


# --- Subscriptions (billing view: country, currency, tax, invoices) -----------------------------


class AdminSubscriptionSerializer(serializers.ModelSerializer):
    """GET /api/admin/subscriptions/: one customer's subscription with what they owe and their invoices."""

    owner = serializers.SerializerMethodField()
    company = serializers.SerializerMethodField()
    region = serializers.SerializerMethodField()
    plan = serializers.SerializerMethodField()
    amount_due = serializers.SerializerMethodField()
    days_left = serializers.SerializerMethodField()
    latest_payment = serializers.SerializerMethodField()
    invoices = serializers.SerializerMethodField()

    class Meta:
        model = Subscription
        fields = (
            "id", "owner", "company", "region", "plan", "status", "currency", "billing_cycle", "amount_inr",
            "amount_due", "is_trial", "starts_at", "ends_at", "days_left", "payment_link", "latest_payment",
            "invoices", "created_at",
        )
        read_only_fields = [f for f in fields if f != "payment_link"]

    def get_owner(self, obj: Subscription) -> dict[str, Any]:
        return {"id": obj.owner_id, "email": obj.owner.email, "name": obj.owner.full_name}

    def get_company(self, obj: Subscription) -> dict[str, str] | None:
        c = obj.company
        return None if c is None else {"name": c.name, "country": c.country, "state": c.state, "gstin": c.gstin}

    def get_region(self, obj: Subscription) -> str:
        return "IN" if obj.currency == Subscription.Currency.INR else "INTL"

    def get_plan(self, obj: Subscription) -> dict[str, str]:
        return {"code": obj.plan.code, "name": obj.plan.name}

    def get_amount_due(self, obj: Subscription) -> dict[str, Any] | None:
        """The next payment (with tax) — what to put on a Skydo payment link for this customer."""
        if obj.status == Subscription.Status.CANCELLED or not obj.plan.requires_payment:
            return None
        return billing.amount_due(obj).as_dict()

    def get_days_left(self, obj: Subscription) -> int | None:
        return billing.days_left(obj)

    def get_latest_payment(self, obj: Subscription) -> dict[str, Any] | None:
        p = billing.latest_payment(obj)
        if p is None:
            return None
        return {
            "id": p.id, "status": p.status, "method": p.get_method_display(), "amount": str(p.amount_inr),
            "currency": p.currency, "submitted_at": p.submitted_at,
        }

    def get_invoices(self, obj: Subscription) -> list[dict[str, Any]]:
        return [
            {"payment_id": inv.payment_id, "number": inv.number, "issued_at": inv.issued_at,
             "total": str(inv.total), "currency": inv.currency}
            for inv in Invoice.objects.filter(payment__subscription=obj).order_by("-issued_at")
        ]

    def validate_payment_link(self, value: str) -> str:
        value = (value or "").strip()
        if value and not value.startswith("https://"):
            raise serializers.ValidationError("Paste the full https:// payment link.")
        return value
