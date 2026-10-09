"""Accounts, plans and early-access registrations."""
from __future__ import annotations

import os
import uuid
from decimal import Decimal
from typing import Any

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager):
    """Manager for the email-based `User` (there is no username)."""

    use_in_migrations = True

    def _create_user(self, email: str, password: str | None, **extra: Any) -> "User":
        if not email:
            raise ValueError("An email address is required.")
        email = self.normalize_email(email).strip().lower()
        user = self.model(email=email, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra: Any) -> "User":
        """Create a regular, active, non-staff user."""
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email: str, password: str | None = None, **extra: Any) -> "User":
        """Create a staff superuser (used by `createsuperuser`)."""
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        # Staff accounts are made by an operator, so their email counts as verified.
        extra.setdefault("email_verified_at", timezone.now())
        if extra.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    """A Zinwe account. Identified by email (stored lowercased)."""

    email = models.EmailField("email address", unique=True)
    full_name = models.CharField(max_length=150, blank=True)
    company = models.CharField(max_length=160, blank=True)
    phone = models.CharField(max_length=32, blank=True)
    is_staff = models.BooleanField(
        default=False, help_text="Can log into the admin site."
    )
    is_active = models.BooleanField(default=True)
    date_joined = models.DateTimeField(default=timezone.now)
    email_verified_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When the owner proved they control this email (signup code). Empty = unverified.",
    )

    objects = UserManager()

    EMAIL_FIELD = "email"
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        ordering = ["-date_joined"]

    def __str__(self) -> str:
        return self.email

    def clean(self) -> None:
        super().clean()
        self.email = self.__class__.objects.normalize_email(self.email).strip().lower()

    @property
    def email_verified(self) -> bool:
        return self.email_verified_at is not None

    @property
    def is_pending_verification(self) -> bool:
        """Signed up but never entered the emailed code (cannot log in yet)."""
        return not self.is_active and self.email_verified_at is None

    def get_full_name(self) -> str:
        return self.full_name or self.email

    def get_short_name(self) -> str:
        return (self.full_name.split(" ")[0] if self.full_name else "") or self.email

    @property
    def active_plan_activation(self) -> "PlanActivation | None":
        """The user's current active activation, if any."""
        return (
            self.plan_activations.select_related("plan")
            .filter(is_active=True)
            .order_by("-starts_at")
            .first()
        )


class Plan(models.Model):
    """A product plan. Seeded by migration 0002, catalogue updated by 0005."""

    FREE = "free"
    EARLY_ACCESS = "early-access"
    PRO = "pro"

    code = models.SlugField(max_length=50, unique=True)
    name = models.CharField(max_length=120)
    price_inr_monthly = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    trial_days = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    tagline = models.CharField(max_length=160, blank=True)
    max_devices = models.PositiveIntegerField(default=0, help_text="How many PCs the plan covers.")
    features = models.JSONField(default=list, blank=True, help_text="Short feature lines shown on plan cards.")
    is_recommended = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sort_order", "code"]

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"

    @property
    def requires_payment(self) -> bool:
        return (self.price_inr_monthly or Decimal("0")) > 0


class PlanActivation(models.Model):
    """A user's subscription to a plan. At most one active row per user."""

    user = models.ForeignKey(
        "z_auth.User", on_delete=models.CASCADE, related_name="plan_activations"
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="activations")
    starts_at = models.DateTimeField(default=timezone.now)
    ends_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    source = models.CharField(
        max_length=40, blank=True, default="self-serve",
        help_text="How it was activated, e.g. self-serve, early-access, admin.",
    )

    class Meta:
        ordering = ["-starts_at"]
        indexes = [models.Index(fields=["user", "is_active"])]

    def __str__(self) -> str:
        return f"{self.user} -> {self.plan.code}"


class EarlyAccessRegistration(models.Model):
    """A pre-launch waitlist sign-up from the website's early-access form."""

    class TeamSize(models.TextChoices):
        S_1_10 = "1-10", "1-10"
        S_11_25 = "11-25", "11-25"
        S_26_50 = "26-50", "26-50"
        S_50_PLUS = "50+", "50+"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        INVITED = "invited", "Invited"
        ACTIVATED = "activated", "Activated"
        DECLINED = "declined", "Declined"

    PLATFORM_CHOICES = ("windows", "linux", "macos")
    CONCERN_CHOICES = ("usb", "remote", "installs", "email", "compliance")

    name = models.CharField(max_length=120)
    email = models.EmailField(unique=True, help_text="Stored lowercased.")
    company = models.CharField(max_length=160)
    phone = models.CharField(max_length=32, blank=True)
    team_size = models.CharField(max_length=10, choices=TeamSize.choices)
    platforms = models.JSONField(default=list, blank=True)
    concerns = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    plan = models.ForeignKey(
        Plan, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="early_access_registrations",
    )
    trial_days = models.PositiveIntegerField(default=90)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["created_at"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self) -> str:
        return f"{self.email} ({self.company})"

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.email = (self.email or "").strip().lower()
        super().save(*args, **kwargs)


# --- Owner portal: one-time codes, company, subscription, manual payments -------


class OneTimeCode(models.Model):
    """
    A 6-digit email code. Only an HMAC of the code is stored (see `z_auth.otp`).

    Rows are also written for addresses without an account, so cooldowns and
    hourly caps behave the same either way (no account enumeration).
    """

    class Purpose(models.TextChoices):
        SIGNUP = "signup", "Sign-up"
        LOGIN = "login", "Sign-in"
        PASSWORD_RESET = "password_reset", "Password reset"

    email = models.EmailField(help_text="Stored lowercased.")
    purpose = models.CharField(max_length=20, choices=Purpose.choices)
    code_hash = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["email", "purpose", "created_at"])]

    def __str__(self) -> str:
        return f"{self.purpose} code for {self.email}"


class Company(models.Model):
    """The owner's business. One per owner in v1 (team members come later)."""

    class Industry(models.TextChoices):
        IT_SERVICES = "it_services", "IT services"
        MANUFACTURING = "manufacturing", "Manufacturing"
        FINANCE = "finance", "Finance"
        HEALTHCARE = "healthcare", "Healthcare"
        EDUCATION = "education", "Education"
        RETAIL = "retail", "Retail"
        PROFESSIONAL_SERVICES = "professional_services", "Professional services"
        OTHER = "other", "Other"

    TeamSize = EarlyAccessRegistration.TeamSize

    owner = models.OneToOneField(
        "z_auth.User", on_delete=models.CASCADE, related_name="owned_company"
    )
    name = models.CharField(max_length=160)
    industry = models.CharField(max_length=40, choices=Industry.choices)
    team_size = models.CharField(max_length=10, choices=TeamSize.choices)
    phone = models.CharField(max_length=32)
    city = models.CharField(max_length=80)
    state = models.CharField(max_length=80)
    country = models.CharField(max_length=80, default="India")
    gstin = models.CharField("GSTIN", max_length=15, blank=True)
    website = models.URLField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "companies"

    def __str__(self) -> str:
        return self.name


class Subscription(models.Model):
    """
    An owner's subscription to a plan. The newest row is the current one.

    Free/trial plans start `active`. Paid plans start `pending_payment` and go
    `active` when staff verify a `ManualPayment` in the admin.
    """

    class Status(models.TextChoices):
        PENDING_PAYMENT = "pending_payment", "Pending payment"
        ACTIVE = "active", "Active"
        EXPIRED = "expired", "Expired"
        CANCELLED = "cancelled", "Cancelled"

    class BillingCycle(models.TextChoices):
        MONTHLY = "monthly", "Monthly"

    owner = models.ForeignKey(
        "z_auth.User", on_delete=models.CASCADE, related_name="subscriptions"
    )
    company = models.ForeignKey(
        Company, null=True, blank=True, on_delete=models.SET_NULL, related_name="subscriptions"
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING_PAYMENT, db_index=True
    )
    amount_inr = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    billing_cycle = models.CharField(
        max_length=20, choices=BillingCycle.choices, default=BillingCycle.MONTHLY
    )
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    activation = models.ForeignKey(
        PlanActivation, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="subscriptions", help_text="The PlanActivation created when this went active.",
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["owner", "created_at"])]

    def __str__(self) -> str:
        return f"{self.owner} -> {self.plan.code} ({self.status})"


def payment_proof_upload_to(instance: "ManualPayment", filename: str) -> str:
    """payments/<yyyy>/<mm>/<random>.<ext> -- never trust the uploaded file name."""
    ext = os.path.splitext(filename)[1].lower()[:8]
    now = timezone.now()
    return f"payments/{now:%Y}/{now:%m}/{uuid.uuid4().hex}{ext}"


class ManualPayment(models.Model):
    """A payment the owner made outside the app (UPI, bank transfer...), checked by staff."""

    class Method(models.TextChoices):
        UPI = "upi", "UPI"
        BANK_TRANSFER = "bank_transfer", "Bank transfer"
        CHEQUE = "cheque", "Cheque"
        CASH = "cash", "Cash"

    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Submitted"
        VERIFIED = "verified", "Verified"
        REJECTED = "rejected", "Rejected"

    subscription = models.ForeignKey(
        Subscription, on_delete=models.CASCADE, related_name="payments"
    )
    method = models.CharField(max_length=20, choices=Method.choices)
    amount_inr = models.DecimalField(max_digits=10, decimal_places=2)
    reference = models.CharField(max_length=64, help_text="UTR / transaction id / cheque number.")
    paid_on = models.DateField()
    payer_name = models.CharField(max_length=120)
    proof = models.FileField(
        upload_to=payment_proof_upload_to, blank=True,
        help_text="Screenshot or receipt (JPG, PNG or PDF, up to 5 MB). Staff-only download.",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.SUBMITTED, db_index=True
    )
    rejection_reason = models.TextField(blank=True)
    submitted_at = models.DateTimeField(default=timezone.now)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        "z_auth.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="reviewed_payments",
    )

    class Meta:
        ordering = ["-submitted_at", "-id"]

    def __str__(self) -> str:
        return f"Payment #{self.pk} {self.amount_inr} INR ({self.status})"

    @property
    def owner(self) -> User:
        return self.subscription.owner
