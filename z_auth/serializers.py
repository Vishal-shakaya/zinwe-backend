"""Request validation and response shaping for z_auth. No business logic here."""
from __future__ import annotations

from typing import Any, Iterable

from django.contrib.auth import password_validation
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from rest_framework import serializers

from .models import EarlyAccessRegistration as EAR
from .models import Plan, PlanActivation, User

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
        "message": "Spot reserved." if created else "You're already on the list.",
    }


# --- Accounts ---------------------------------------------------------------


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "email", "full_name", "company", "phone", "is_staff", "date_joined")
        read_only_fields = fields


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


# --- Plans ------------------------------------------------------------------


class PlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = Plan
        fields = ("code", "name", "price_inr_monthly", "trial_days")
        read_only_fields = fields


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
