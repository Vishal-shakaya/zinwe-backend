"""Validation and response shapes for the owner portal: company, subscription, payments."""
from __future__ import annotations

import os
import re
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.uploadedfile import UploadedFile
from django.core.validators import URLValidator
from django.utils import timezone
from rest_framework import serializers

from .billing import amount_due, can_renew, days_left, latest_payment, renewal_period
from .models import Company, Feedback, ManualPayment, Plan, Subscription
from .serializers import PICK_ONE, REQUIRED_MESSAGES, PlanSerializer, StrippedChoiceField

GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
GSTIN_EXAMPLE = "07ABCDE1234F1Z5"  # the field's placeholder: never a real customer's GSTIN (J2-01)

# GST state codes (a GSTIN's first two digits) by the state names the company form uses. Some states have
# two: Andhra Pradesh (28 before 2014, 37 after), Dadra and Nagar Haveli and Daman and Diu (25 and 26,
# merged in 2020) and Ladakh (38, earlier 01 under Jammu and Kashmir). Same table as the website's.
GST_STATE_CODES: dict[str, tuple[str, ...]] = {
    "Jammu and Kashmir": ("01",), "Himachal Pradesh": ("02",), "Punjab": ("03",), "Chandigarh": ("04",),
    "Uttarakhand": ("05",), "Haryana": ("06",), "Delhi": ("07",), "Rajasthan": ("08",),
    "Uttar Pradesh": ("09",), "Bihar": ("10",), "Sikkim": ("11",), "Arunachal Pradesh": ("12",),
    "Nagaland": ("13",), "Manipur": ("14",), "Mizoram": ("15",), "Tripura": ("16",), "Meghalaya": ("17",),
    "Assam": ("18",), "West Bengal": ("19",), "Jharkhand": ("20",), "Odisha": ("21",),
    "Chhattisgarh": ("22",), "Madhya Pradesh": ("23",), "Gujarat": ("24",),
    "Dadra and Nagar Haveli and Daman and Diu": ("26", "25"), "Maharashtra": ("27",), "Karnataka": ("29",),
    "Goa": ("30",), "Lakshadweep": ("31",), "Kerala": ("32",), "Tamil Nadu": ("33",), "Puducherry": ("34",),
    "Andaman and Nicobar Islands": ("35",), "Telangana": ("36",), "Andhra Pradesh": ("37", "28"),
    "Ladakh": ("38", "01"),
}


def _state_key(name: str) -> str:
    return " ".join((name or "").lower().replace("&", "and").replace("nct of", "").split())


_CODES_BY_STATE = {_state_key(k): (k, v) for k, v in GST_STATE_CODES.items()}


def gstin_state(gstin: str) -> str | None:
    """The state a GSTIN was issued in (its first two digits), or None."""
    code = gstin[:2]
    for state, codes in GST_STATE_CODES.items():
        if code in codes:
            return state
    return None


def gstin_state_error(gstin: str, state: str) -> str | None:
    """A message when the GSTIN wasn't issued in the company's (Indian) state; None when it fits or is unknown."""
    if not gstin:
        return None
    known = _CODES_BY_STATE.get(_state_key(state))
    if known is None:
        return None
    name, codes = known
    if gstin[:2] in codes:
        return None
    issued_in = gstin_state(gstin)
    if issued_in:
        return f"This GSTIN is from {issued_in}, but you picked {name}. Check the GSTIN or the state."
    return f"This GSTIN doesn't match {name}: GSTINs from {name} start with {codes[0]}."
CHOICE_MESSAGES = {"required": PICK_ONE, "blank": PICK_ONE, "null": PICK_ONE, "invalid_choice": PICK_ONE}


def _text(max_length: int, **kwargs: Any) -> serializers.CharField:
    return serializers.CharField(max_length=max_length, error_messages=REQUIRED_MESSAGES, **kwargs)


# --- Company ------------------------------------------------------------------


class CompanySerializer(serializers.ModelSerializer):
    """Company in/out (portal-v1 §3.5). PATCH is partial."""

    name = _text(160)
    industry = StrippedChoiceField(choices=Company.Industry.choices, error_messages=CHOICE_MESSAGES)
    team_size = StrippedChoiceField(choices=Company.TeamSize.choices, error_messages=CHOICE_MESSAGES)
    phone = _text(32)
    city = _text(80)
    state = _text(80)
    country = serializers.CharField(max_length=80, required=False, allow_blank=True, default="India")
    gstin = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")
    website = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")

    class Meta:
        model = Company
        fields = (
            "id", "name", "industry", "team_size", "phone", "city", "state", "country",
            "gstin", "website", "created_at",
        )
        read_only_fields = ("id", "created_at")

    def validate_phone(self, value: str) -> str:
        if len(re.sub(r"\D", "", value)) < 7:
            raise serializers.ValidationError("Enter a valid phone number.")
        return value

    def validate_country(self, value: str) -> str:
        return value.strip() or "India"

    def validate_gstin(self, value: str) -> str:
        value = re.sub(r"\s", "", value or "").upper()
        if value and not GSTIN_RE.match(value):
            raise serializers.ValidationError(
                "Enter a valid 15-character GSTIN, e.g. 07ABCDE1234F1Z5."
            )
        if value == GSTIN_EXAMPLE:
            raise serializers.ValidationError("That's the example GSTIN. Enter your own, or leave this empty.")
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """The GSTIN's first two digits must be the company's state's GST code (it decides CGST+SGST vs IGST)."""
        instance = getattr(self, "instance", None)
        gstin = attrs.get("gstin", instance.gstin if instance else "")
        state = attrs.get("state", instance.state if instance else "")
        country = attrs.get("country", instance.country if instance else "India")
        from .pricing import INDIA_NAMES

        if gstin and (country or "").strip().lower() in INDIA_NAMES:
            message = gstin_state_error(gstin, state)
            if message:
                raise serializers.ValidationError({"gstin": [message]})
        return attrs

    def validate_website(self, value: str) -> str:
        value = (value or "").strip()
        if not value:
            return ""
        if "://" not in value:
            value = f"https://{value}"
        try:
            if len(value) > 200:
                raise DjangoValidationError("too long")
            URLValidator(schemes=["http", "https"])(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                "Enter a valid website address, e.g. https://example.com."
            ) from exc
        return value


# --- Subscription + payments --------------------------------------------------


class PaymentSerializer(serializers.ModelSerializer):
    has_proof = serializers.SerializerMethodField()
    # The plan this payment was for, as quoted when it was made (not the subscription's plan today: an
    # upgraded subscription would relabel its old payments).
    plan_name = serializers.SerializerMethodField()
    upgrade_plan_name = serializers.CharField(source="upgrade_plan.name", read_only=True, default=None)
    invoice_number = serializers.SerializerMethodField()

    def get_plan_name(self, payment: ManualPayment) -> str:
        due_name = (payment.due or {}).get("plan_name")
        if due_name:
            return due_name
        return (payment.upgrade_plan or payment.subscription.plan).name

    class Meta:
        model = ManualPayment
        fields = (
            "id", "method", "amount_inr", "currency", "due", "reference", "paid_on", "payer_name", "status",
            "rejection_reason", "has_proof", "submitted_at", "reviewed_at",
            "is_renewal", "covers_from", "covers_until", "plan_name", "upgrade_plan_name", "invoice_number",
        )
        read_only_fields = fields

    def get_has_proof(self, obj: ManualPayment) -> bool:
        return bool(obj.proof)

    def get_invoice_number(self, obj: ManualPayment) -> str | None:
        invoice = getattr(obj, "invoice", None) if obj.pk else None
        return invoice.number if invoice is not None else None


class SubscriptionSerializer(serializers.ModelSerializer):
    plan = PlanSerializer(read_only=True)
    latest_payment = serializers.SerializerMethodField()
    days_left = serializers.SerializerMethodField()
    can_renew = serializers.SerializerMethodField()
    renewal_period = serializers.SerializerMethodField()
    amount_due = serializers.SerializerMethodField()

    class Meta:
        model = Subscription
        fields = (
            "id", "plan", "status", "amount_inr", "currency", "billing_cycle", "payment_link", "starts_at",
            "ends_at", "created_at", "latest_payment", "is_trial", "days_left", "can_renew", "renewal_period",
            "amount_due",
        )
        read_only_fields = fields

    def get_amount_due(self, obj: Subscription) -> dict[str, Any] | None:
        """The next payment (plan for one billing cycle + tax), while one can be made: first or renewal."""
        if obj.status == Subscription.Status.PENDING_PAYMENT or can_renew(obj):
            return amount_due(obj).as_dict()
        return None

    def get_latest_payment(self, obj: Subscription) -> dict[str, Any] | None:
        payment = latest_payment(obj)
        return PaymentSerializer(payment).data if payment else None

    def get_days_left(self, obj: Subscription) -> int | None:
        """Whole days until an active plan ends (0 = today); None when open-ended."""
        return days_left(obj)

    def get_can_renew(self, obj: Subscription) -> bool:
        """An active paid plan with no payment under review: the owner can pay for next month now."""
        return can_renew(obj)

    def get_renewal_period(self, obj: Subscription) -> dict[str, Any] | None:
        """The period a renewal paid now would add (from the current end date: no days lost)."""
        if not can_renew(obj):
            return None
        start, end = renewal_period(obj)
        as_text = serializers.DateTimeField().to_representation  # same format as ends_at
        return {"from": as_text(start), "until": as_text(end)}


class SubscribeSerializer(serializers.Serializer):
    plan = serializers.SlugRelatedField(
        slug_field="code",
        queryset=Plan.objects.filter(is_active=True),
        error_messages={
            **REQUIRED_MESSAGES,
            "does_not_exist": "That plan doesn't exist or isn't available.",
            "invalid": "That plan doesn't exist or isn't available.",
        },
    )


PROOF_TYPES = {
    # extension -> accepted leading bytes
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".pdf": (b"%PDF-",),
}
BAD_PROOF_TYPE = "Upload a JPG, PNG or PDF file."


class PaymentSubmitSerializer(serializers.Serializer):
    """
    Multipart form for `POST subscription/payment/`.

    The payment page sends just the receipt (`proof`), the way the owner paid (`method`) and the terms
    tick; billing fills in the amount due, today and the owner's name. Without a receipt, a `reference`
    is required instead (the full form, still accepted).
    """

    method = StrippedChoiceField(
        choices=ManualPayment.Method.choices, error_messages=CHOICE_MESSAGES, required=False,
    )
    amount_inr = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal("1"), required=False,
        error_messages={
            **REQUIRED_MESSAGES,
            "invalid": "Enter the amount you paid, e.g. 1999.",
            "min_value": "Enter the amount you paid, e.g. 1999.",
            "max_digits": "That amount is too large.",
            "max_whole_digits": "That amount is too large.",
            "max_decimal_places": "Use at most 2 decimal places.",
        },
    )
    reference = _text(64, required=False, allow_blank=True)
    paid_on = serializers.DateField(
        required=False, error_messages={**REQUIRED_MESSAGES, "invalid": "Enter the date as YYYY-MM-DD."}
    )
    payer_name = _text(120, required=False, allow_blank=True)
    # The payment-terms checkbox (refund policy and billing terms). Must be ticked.
    accept_terms = serializers.BooleanField(
        error_messages={"required": "Please accept the payment terms to continue.", "invalid": "Please accept the payment terms to continue."},
    )
    # Dashboard upgrade: the plan (by code) this payment moves the owner up to.
    upgrade_plan = serializers.SlugRelatedField(
        slug_field="code", queryset=Plan.objects.filter(is_active=True), required=False, allow_null=True,
        error_messages={"does_not_exist": "That plan isn't available.", "invalid": "That plan isn't available."},
    )
    proof = serializers.FileField(
        required=False, allow_null=True, allow_empty_file=False,
        error_messages={
            "invalid": BAD_PROOF_TYPE,
            "empty": "That file is empty.",
            "no_name": BAD_PROOF_TYPE,
        },
    )

    def validate_accept_terms(self, value: bool) -> bool:
        if value is not True:
            raise serializers.ValidationError("Please accept the payment terms to continue.")
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if not attrs.get("proof") and not (attrs.get("reference") or "").strip():
            raise serializers.ValidationError({"proof": ["Upload your payment receipt or screenshot."]})
        return attrs

    def validate_paid_on(self, value: Any) -> Any:
        if value > timezone.localdate():
            raise serializers.ValidationError("The payment date can't be in the future.")
        return value

    def validate_proof(self, value: UploadedFile | None) -> UploadedFile | None:
        if value is None:
            return None
        if value.size > settings.ZINWE_PAYMENT_PROOF_MAX_BYTES:
            limit_mb = settings.ZINWE_PAYMENT_PROOF_MAX_BYTES // (1024 * 1024)
            raise serializers.ValidationError(f"The file is too large. The limit is {limit_mb} MB.")
        ext = os.path.splitext(value.name or "")[1].lower()
        signatures = PROOF_TYPES.get(ext)
        if not signatures:
            raise serializers.ValidationError(BAD_PROOF_TYPE)
        head = value.read(16)
        value.seek(0)
        if not any(head.startswith(sig) for sig in signatures):
            raise serializers.ValidationError(BAD_PROOF_TYPE)
        return value


class QuoteEmailSerializer(serializers.Serializer):
    """`POST subscription/quote/email/`: who gets the quote (blank = the owner's own email)."""

    email = serializers.EmailField(
        max_length=254, required=False, allow_blank=True,
        error_messages={"invalid": "Enter a valid email address."},
    )


class FeedbackSerializer(serializers.ModelSerializer):
    """POST /api/auth/feedback/ in, and the owner's own past feedback out."""

    rating = serializers.IntegerField(
        min_value=Feedback.RATING_MIN, max_value=Feedback.RATING_MAX,
        error_messages={
            "required": "Pick a rating from 1 to 10.",
            "min_value": "Pick a rating from 1 to 10.",
            "max_value": "Pick a rating from 1 to 10.",
            "invalid": "Pick a rating from 1 to 10.",
        },
    )
    message = serializers.CharField(required=False, allow_blank=True, max_length=2000, trim_whitespace=True)
    page = serializers.CharField(required=False, allow_blank=True, max_length=120)

    class Meta:
        model = Feedback
        fields = ["id", "rating", "message", "page", "created_at"]
        read_only_fields = ["id", "created_at"]
