"""Shapes for the internal admin API. Output serializers read services' views / models."""
from __future__ import annotations

from typing import Any

from rest_framework import serializers

from z_auth import billing
from z_auth.models import Feedback, ManualPayment
from z_notification.models import Notification


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
        # The period it paid for (approved), or would pay for if approved now (waiting).
        "covers_from": pay.covers_from,
        "covers_until": pay.covers_until,
        "would_cover": _would_cover(pay),
    }


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
                }
                for m in obj.members
            ],
        }

    def get_emails(self, obj: Any) -> dict[str, Any]:
        """Custom emails the team sent this customer: how many, and when the last went."""
        return {"sent": obj.emails_sent, "last_sent_at": obj.last_email_at}

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

    class Meta:
        model = Feedback
        fields = ["id", "rating", "message", "page", "created_at", "customer"]

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


class ChangePlanSerializer(serializers.Serializer):
    plan = serializers.SlugField()


class SetPasswordSerializer(serializers.Serializer):
    password = serializers.CharField(max_length=128, trim_whitespace=False)


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
