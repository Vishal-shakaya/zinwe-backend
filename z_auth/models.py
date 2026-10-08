"""Accounts, plans and early-access registrations."""
from __future__ import annotations

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
    """A product plan. Seeded by migration 0002 (free / early-access / pro)."""

    FREE = "free"
    EARLY_ACCESS = "early-access"
    PRO = "pro"

    code = models.SlugField(max_length=50, unique=True)
    name = models.CharField(max_length=120)
    price_inr_monthly = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    trial_days = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["price_inr_monthly", "code"]

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"


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
