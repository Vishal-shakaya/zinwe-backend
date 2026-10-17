"""Request validation and response shaping for z_auth. No business logic here."""
from __future__ import annotations

import re
from typing import Any, Iterable

from django.conf import settings
from django.contrib.auth import password_validation
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from rest_framework import serializers

from .models import EarlyAccessRegistration as EAR
from .models import OneTimeCode, Plan, PlanActivation, User
from .services import trial_available

REQUIRED = "This field is required."
PICK_ONE = "Pick one option."


class StrippedChoiceField(serializers.ChoiceField):
    """ChoiceField that ignores surrounding whitespace."""

    def to_internal_value(self, data: Any) -> Any:
        if isinstance(data, str):
            data = data.strip()
        return super().to_internal_value(data)


class OptionListField(serializers.Field):
    """
    A list of string options from a fixed set.

    Values are stripped, lowercased and de-duplicated (order kept). Unknown
    values produce flat, human messages on this field.
    """

    default_error_messages = {
        "not_a_list": "Send a list of options (it can be empty).",
        "null": "Send a list of options (it can be empty).",
    }

    def __init__(self, options: Iterable[str], **kwargs: Any) -> None:
        self.options = tuple(options)
        kwargs.setdefault("required", False)
        kwargs.setdefault("default", list)
        super().__init__(**kwargs)

    def to_internal_value(self, data: Any) -> list[str]:
        if not isinstance(data, list):
            self.fail("not_a_list")
        cleaned: list[str] = []
        errors: list[str] = []
        allowed = ", ".join(self.options)
        for item in data:
            value = item.strip().lower() if isinstance(item, str) else None
            if value not in self.options:
                errors.append(f'"{item}" is not a valid option. Choose from: {allowed}.')
            elif value not in cleaned:
                cleaned.append(value)
        if errors:
            raise serializers.ValidationError(errors)
        return cleaned

    def to_representation(self, value: Any) -> list[str]:
        return list(value or [])


class EarlyAccessSerializer(serializers.Serializer):
    """Validates the public early-access form (contract: see README)."""

    name = serializers.CharField(
        max_length=120, error_messages={"required": REQUIRED, "blank": REQUIRED, "null": REQUIRED}
    )
    email = serializers.EmailField(
        max_length=254,
        error_messages={
            "required": REQUIRED,
            "blank": REQUIRED,
            "null": REQUIRED,
            "invalid": "Enter a valid email address.",
        },
    )
    company = serializers.CharField(
        max_length=160, error_messages={"required": REQUIRED, "blank": REQUIRED, "null": REQUIRED}
    )
    phone = serializers.CharField(
        max_length=32, required=False, allow_blank=True, allow_null=True, default=""
    )
    team_size = StrippedChoiceField(
        choices=EAR.TeamSize.choices,
        error_messages={
            "required": PICK_ONE,
            "null": PICK_ONE,
            "blank": PICK_ONE,
            "invalid_choice": PICK_ONE,
        },
    )
    platforms = OptionListField(EAR.PLATFORM_CHOICES)
    concerns = OptionListField(EAR.CONCERN_CHOICES)

    def validate_email(self, value: str) -> str:
        return value.strip().lower()

    def validate_phone(self, value: str | None) -> str:
        return (value or "").strip()


def early_access_payload(registration: EAR, created: bool) -> dict[str, Any]:
    """Response body for both the 201 (new) and 200 (duplicate) cases."""
    return {
        "id": registration.id,
        "email": registration.email,
        "status": registration.status,
        "trial_days": registration.trial_days,
        "already_registered": not created,
        "message": "Setup call booked." if created else "You've already booked a setup call.",
    }


# --- Accounts ---------------------------------------------------------------


class UserSerializer(serializers.ModelSerializer):
    """The `User` shape of portal-v1 §3.1 (the company lives on `Company` now)."""

    email_verified = serializers.BooleanField(read_only=True)
    terms = serializers.SerializerMethodField()
    tour_done = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id", "email", "full_name", "phone", "is_staff", "date_joined", "email_verified", "terms", "tour_done",
        )
        read_only_fields = fields

    def get_tour_done(self, user: User) -> bool:
        """The dashboard tour runs once per account (not per browser)."""
        return user.tour_done_at is not None

    def get_terms(self, user: User) -> dict[str, Any]:
        """Which Terms version they accepted, and whether they still need to accept the current one."""
        current = settings.ZINWE_LEGAL_VERSION
        return {
            "version": user.terms_version,
            "accepted_at": user.terms_accepted_at,
            "current_version": current,
            "needs_acceptance": user.terms_version != current,
        }


class TermsAcceptSerializer(serializers.Serializer):
    """The one-time "accept the updated terms" prompt: the box must be ticked."""

    accept_terms = serializers.BooleanField(
        error_messages={"required": "Please accept the updated Terms to continue.", "invalid": "Please accept the updated Terms to continue."},
    )

    def validate_accept_terms(self, value: bool) -> bool:
        if value is not True:
            raise serializers.ValidationError("Please accept the updated Terms to continue.")
        return value


class RegisterSerializer(serializers.Serializer):
    email = serializers.EmailField(
        max_length=254, error_messages={"invalid": "Enter a valid email address."}
    )
    password = serializers.CharField(write_only=True, trim_whitespace=False, max_length=128)
    full_name = serializers.CharField(max_length=150)
    company = serializers.CharField(max_length=160, required=False, allow_blank=True, default="")
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")

    def validate_email(self, value: str) -> str:
        value = value.strip().lower()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        candidate = User(email=attrs["email"], full_name=attrs["full_name"])
        try:
            password_validation.validate_password(attrs["password"], user=candidate)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)}) from exc
        return attrs


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField(error_messages={"invalid": "Enter a valid email address."})
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_email(self, value: str) -> str:
        return value.strip().lower()


# --- Sign-up / codes / password reset ----------------------------------------

REQUIRED_MESSAGES = {"required": REQUIRED, "blank": REQUIRED, "null": REQUIRED}
EMAIL_MESSAGES = {**REQUIRED_MESSAGES, "invalid": "Enter a valid email address."}
BAD_CODE = "That code is incorrect or has expired."


def email_field() -> serializers.EmailField:
    return serializers.EmailField(max_length=254, error_messages=EMAIL_MESSAGES)


class CodeField(serializers.CharField):
    """A 6-digit code; spaces are ignored. Malformed input gets the same message as a wrong code."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("max_length", 20)
        kwargs.setdefault("error_messages", {**REQUIRED_MESSAGES, "max_length": BAD_CODE})
        super().__init__(**kwargs)

    def to_internal_value(self, data: Any) -> str:
        value = re.sub(r"\s", "", str(super().to_internal_value(data)))
        if not re.fullmatch(r"\d{6}", value):
            raise serializers.ValidationError(BAD_CODE)
        return value


def _check_password(password: str, user: User, field: str) -> None:
    try:
        password_validation.validate_password(password, user=user)
    except DjangoValidationError as exc:
        raise serializers.ValidationError({field: list(exc.messages)}) from exc


class SignupSerializer(serializers.Serializer):
    full_name = serializers.CharField(max_length=150, error_messages=REQUIRED_MESSAGES)
    email = email_field()
    password = serializers.CharField(
        write_only=True, trim_whitespace=False, max_length=128, error_messages=REQUIRED_MESSAGES
    )
    # The sign-up checkbox: Terms and Conditions + Privacy Policy (must be ticked).
    accept_terms = serializers.BooleanField(
        error_messages={"required": "Please accept the Terms and Conditions and Privacy Policy to continue.", "invalid": "Please accept the Terms and Conditions and Privacy Policy to continue."},
    )

    def validate_accept_terms(self, value: bool) -> bool:
        if value is not True:
            raise serializers.ValidationError("Please accept the Terms and Conditions and Privacy Policy to continue.")
        return value

    def validate_email(self, value: str) -> str:
        value = value.strip().lower()
        existing = User.objects.filter(email__iexact=value).first()
        if existing is not None and not existing.is_pending_verification:
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        _check_password(
            attrs["password"], User(email=attrs["email"], full_name=attrs["full_name"]), "password"
        )
        return attrs


class EmailSerializer(serializers.Serializer):
    email = email_field()

    def validate_email(self, value: str) -> str:
        return value.strip().lower()


class EmailCodeSerializer(EmailSerializer):
    code = CodeField()


class ResendCodeSerializer(EmailSerializer):
    purpose = StrippedChoiceField(
        choices=OneTimeCode.Purpose.choices,
        error_messages={**REQUIRED_MESSAGES, "invalid_choice": PICK_ONE},
    )


class PasswordResetSerializer(EmailCodeSerializer):
    new_password = serializers.CharField(
        write_only=True, trim_whitespace=False, max_length=128, error_messages=REQUIRED_MESSAGES
    )


class PasswordChangeSerializer(serializers.Serializer):
    current_password = serializers.CharField(
        write_only=True, trim_whitespace=False, max_length=128, error_messages=REQUIRED_MESSAGES
    )
    new_password = serializers.CharField(
        write_only=True, trim_whitespace=False, max_length=128, error_messages=REQUIRED_MESSAGES
    )


# --- Plans ------------------------------------------------------------------


class PlanSerializer(serializers.ModelSerializer):
    """
    Plan (portal-v1 §3.6). `trial_available` is per caller: pass the user as
    `context={"user": ...}`; without one (or anonymous) every trial counts as available.
    """

    requires_payment = serializers.BooleanField(read_only=True)
    trial_available = serializers.SerializerMethodField()
    quote = serializers.SerializerMethodField()
    gst_charged = serializers.SerializerMethodField()

    class Meta:
        model = Plan
        fields = (
            "code", "name", "tagline", "price_inr_monthly", "list_price_inr_monthly", "discount_percent",
            "price_usd_monthly", "list_price_usd_monthly", "quote",
            "trial_days", "max_devices", "history_days",
            "features", "modules", "is_recommended", "requires_payment", "sort_order", "trial_available",
            "gst_charged",
        )
        read_only_fields = fields

    def get_trial_available(self, obj: Plan) -> bool:
        return trial_available(obj, self.context.get("user"))

    def _caller_company(self) -> Any:
        """The signed-in caller's company (None for visitors / no company yet), looked up once per response."""
        from .models import Company

        if "_company" not in self.context:
            user = self.context.get("user")
            authed = user is not None and getattr(user, "is_authenticated", False)
            self.context["_company"] = Company.objects.filter(owner=user).first() if authed else None
        return self.context["_company"]

    def get_gst_charged(self, obj: Plan) -> bool:
        """
        Whether "+ GST" applies to this caller: the seller GSTIN is set and the caller is billed in India
        (visitors and owners without a company count as India). All "+ GST" copy is driven by this.
        """
        from . import pricing

        if not (settings.ZINWE_INVOICE_SELLER.get("gstin") or "").strip():
            return False
        return pricing.is_india(self._caller_company())

    def get_quote(self, obj: Plan) -> dict[str, Any] | None:
        """
        What the signed-in owner would pay for this plan (their country's currency, cycle and tax).
        None for visitors and owners without a company yet: the cards then show both currencies.
        """
        from . import pricing

        company = self._caller_company()
        if company is None:
            return None
        return pricing.quote(obj, company).as_dict()


class PlanActivationSerializer(serializers.ModelSerializer):
    plan = PlanSerializer(read_only=True)
    days_left = serializers.SerializerMethodField()

    class Meta:
        model = PlanActivation
        fields = ("id", "plan", "starts_at", "ends_at", "is_active", "source", "days_left")
        read_only_fields = fields

    def get_days_left(self, obj: PlanActivation) -> int | None:
        """Whole days until `ends_at`; None for open-ended plans."""
        if obj.ends_at is None:
            return None
        return max(0, (obj.ends_at - timezone.now()).days)


class PlanActivateSerializer(serializers.Serializer):
    plan = serializers.SlugRelatedField(
        slug_field="code",
        queryset=Plan.objects.filter(is_active=True),
        error_messages={
            "does_not_exist": "That plan doesn't exist or isn't available.",
            "required": REQUIRED,
        },
    )
