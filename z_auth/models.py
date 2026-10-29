"""Accounts, plans and early-access registrations."""
from __future__ import annotations

import os
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.core.validators import MaxValueValidator, MinValueValidator
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
    # Where the sign-up came from when we know it, e.g. "tlf:ep07:email" (The Last File newsletter, Episode 7,
    # from an episode email) or "tlf:series:ref". Set once, at the first sign-up.
    signup_source = models.CharField(max_length=120, blank=True, default="")
    email_verified_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When the owner proved they control this email (signup code). Empty = unverified.",
    )
    terms_accepted_at = models.DateTimeField(
        null=True, blank=True, help_text="When the owner accepted the Terms and Privacy Policy (sign-up checkbox).",
    )
    terms_version = models.CharField(
        max_length=20, blank=True, help_text="Which version of the Terms they accepted (ZINWE_LEGAL_VERSION).",
    )
    tour_done_at = models.DateTimeField(
        null=True, blank=True, help_text="When the owner finished or skipped the dashboard tour (shown once per account).",
    )
    # FR-38: the last social provider used to sign in ("google" / "linkedin"; empty = email only) and the
    # profile photo it gave us, for the onboarding "Signed in with …" card.
    auth_provider = models.CharField(max_length=20, blank=True, default="")
    avatar_url = models.URLField(max_length=500, blank=True, default="")

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
    """A product plan. Seeded by migration 0002, catalogue updated by 0005 and 0006."""

    FREE = "free"
    STARTUPS = "startups"
    TEAMS = "teams"  # 25 PCs, between Startups and Businesses (migration 0024)
    BUSINESSES = "businesses"
    # Legacy (inactive since 0006; rows kept because history points at them).
    EARLY_ACCESS = "early-access"
    PRO = "pro"

    code = models.SlugField(max_length=50, unique=True)
    name = models.CharField(max_length=120)
    price_inr_monthly = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="What the owner pays each month. Worked out from the list price and discount when a list price is set.",
    )
    list_price_inr_monthly = models.DecimalField(
        "list price (INR / month)", max_digits=10, decimal_places=2, default=0,
        help_text="The price before the discount, shown struck through on plan cards. 0 = no discount shown.",
    )
    discount_percent = models.PositiveSmallIntegerField(
        default=0, validators=[MaxValueValidator(90)],
        help_text="Off the list price (0–90). The price owners pay is the list price minus this, rounded to a rupee.",
    )
    # International customers (company outside India) pay in USD, billed yearly (z_auth.pricing).
    price_usd_monthly = models.DecimalField(
        "price (USD / month)", max_digits=10, decimal_places=2, default=0,
        help_text="What a customer outside India pays per month, billed yearly (12 x this). 0 = not sold in USD.",
    )
    list_price_usd_monthly = models.DecimalField(
        "list price (USD / month)", max_digits=10, decimal_places=2, default=0,
        help_text="The USD price before the discount, shown struck through. 0 = no discount shown.",
    )
    # Billed yearly (2026-10-23): the price for the whole year, per currency. Empty = 12 x the monthly price.
    # Every owner chooses monthly or yearly at checkout (z_auth.pricing); the admin app's Pricing page sets these.
    price_inr_yearly = models.DecimalField(
        "price (INR / year)", max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="What an owner in India pays for a whole year, before GST. Empty = 12 x the monthly price.",
    )
    price_usd_yearly = models.DecimalField(
        "price (USD / year)", max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="What an international owner pays for a whole year. Empty = 12 x the USD monthly price.",
    )
    trial_days = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    tagline = models.CharField(max_length=160, blank=True)
    max_devices = models.PositiveIntegerField(default=0, help_text="How many PCs the plan covers.")
    history_days = models.PositiveIntegerField(
        default=0,
        help_text="Days of alerts, activity and work-log history the owner can see (0 = no limit). Older data is "
        "hidden at once and deleted 30 days later (z_devices.retention).",
    )
    features = models.JSONField(default=list, blank=True, help_text="Short feature lines shown on plan cards.")
    modules = models.JSONField(
        default=list, blank=True,
        help_text=(
            "What the plan switches on (enforced): usb_alerts, usb_control, data_transfer, remote_access, "
            "software_installs, web, work_time, app_usage, team_chat, alert_to_employee. See z_auth/entitlements.py."
        ),
    )
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

    @staticmethod
    def discounted(list_price: Decimal, discount_percent: int) -> Decimal:
        """The list price minus the discount, rounded to a whole rupee (half up)."""
        value = Decimal(list_price) * (100 - discount_percent) / 100
        return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP).quantize(Decimal("0.01"))

    def save(self, *args: Any, **kwargs: Any) -> None:
        # A list price makes the paid price follow it: list price minus the discount. Billing only ever
        # reads price_inr_monthly, so a discount is just a lower price with the "was" price shown.
        if self.list_price_inr_monthly and self.list_price_inr_monthly > 0:
            self.price_inr_monthly = self.discounted(self.list_price_inr_monthly, self.discount_percent or 0)
        super().save(*args, **kwargs)


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
    trial_days = models.PositiveIntegerField(default=0)
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

    Free plans and free trials start `active`. Paid plans start `pending_payment`
    and go `active` when staff verify a `ManualPayment` in the admin.
    """

    class Status(models.TextChoices):
        PENDING_PAYMENT = "pending_payment", "Pending payment"
        ACTIVE = "active", "Active"
        EXPIRED = "expired", "Expired"
        CANCELLED = "cancelled", "Cancelled"

    class BillingCycle(models.TextChoices):
        MONTHLY = "monthly", "Monthly"
        YEARLY = "yearly", "Yearly"

    class Currency(models.TextChoices):
        INR = "INR", "Indian rupee"
        USD = "USD", "US dollar"

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
    # Legacy name: the plan price for ONE billing cycle, before tax, in `currency` (INR or USD).
    amount_inr = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="Plan price for one billing cycle, before tax, in the subscription's currency.",
    )
    currency = models.CharField(max_length=3, choices=Currency.choices, default=Currency.INR)
    billing_cycle = models.CharField(
        max_length=20, choices=BillingCycle.choices, default=BillingCycle.MONTHLY
    )
    payment_link = models.URLField(
        max_length=500, blank=True,
        help_text="A payment link made for this customer (e.g. Skydo, card or US bank). Shown on their payment page.",
    )
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    activation = models.ForeignKey(
        PlanActivation, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="subscriptions", help_text="The PlanActivation created when this went active.",
    )
    is_trial = models.BooleanField(
        default=False, help_text="A free trial of a paid plan (one per account, ever).",
    )
    quote_token = models.CharField(
        max_length=64, null=True, blank=True, unique=True, editable=False,
        help_text="Secret for the public quote page (/quote/<token>), made the first time a quote is shared.",
    )
    # The quote as issued (z_auth.quotes): the public link always shows this frozen copy, and says "paid" or
    # "replaced" once it no longer stands, so an emailed quote can never ask for a different amount.
    quote_snapshot = models.JSONField(default=dict, blank=True, editable=False)
    quote_issued_at = models.DateTimeField(null=True, blank=True, editable=False)
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
        PAYPAL = "paypal", "PayPal"  # payments from outside India (legacy)
        SKYDO = "skydo", "Skydo (card or US bank)"  # payments from outside India
        CHEQUE = "cheque", "Cheque"
        CASH = "cash", "Cash"
        # Paid online and confirmed by Razorpay (z_payments): approved automatically, no receipt to check.
        RAZORPAY = "razorpay", "Online (Razorpay)"

    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Submitted"
        VERIFIED = "verified", "Verified"
        REJECTED = "rejected", "Rejected"

    subscription = models.ForeignKey(
        Subscription, on_delete=models.CASCADE, related_name="payments"
    )
    method = models.CharField(max_length=20, choices=Method.choices)
    # Legacy name: the amount the owner says they paid, in `currency`.
    amount_inr = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, choices=Subscription.Currency.choices, default=Subscription.Currency.INR)
    due = models.JSONField(
        default=dict, blank=True,
        help_text="What was due when the owner reported this payment (z_auth.pricing quote: amount, taxes, total).",
    )
    reference = models.CharField(
        max_length=64, blank=True,
        help_text="UTR / transaction id / cheque number. Optional when a receipt is uploaded.",
    )
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
    is_renewal = models.BooleanField(
        default=False, help_text="Paid while the plan was already active, to extend it (renewal).",
    )
    covers_from = models.DateTimeField(
        null=True, blank=True, help_text="Start of the period this payment paid for (set when approved).",
    )
    covers_until = models.DateTimeField(null=True, blank=True, help_text="End of that period.")
    upgrade_plan = models.ForeignKey(
        "z_auth.Plan", null=True, blank=True, on_delete=models.PROTECT, related_name="+",
        help_text="Set when the owner paid to move up to this plan from the dashboard (an upgrade).",
    )
    terms_accepted_at = models.DateTimeField(
        null=True, blank=True, help_text="When the owner accepted the payment terms (refund policy) for this payment.",
    )
    terms_version = models.CharField(max_length=20, blank=True)
    proof_sha256 = models.CharField(
        max_length=64, blank=True, db_index=True,
        help_text="SHA-256 of the uploaded receipt (set on save). Same hash on two payments = same receipt twice.",
    )
    staff_reference = models.CharField(
        max_length=64, blank=True, db_index=True,
        help_text="The UTR / transaction id staff read off the receipt when approving.",
    )
    staff_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="The amount staff read off the receipt when approving (in `currency`).",
    )
    staff_checked_by = models.ForeignKey(
        "z_auth.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="checked_payments", help_text="Staff member who typed the reference and amount.",
    )

    class Meta:
        ordering = ["-submitted_at", "-id"]

    def __str__(self) -> str:
        return f"Payment #{self.pk} {self.amount_inr} INR ({self.status})"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.proof and not self.proof_sha256:
            self.proof_sha256 = self.compute_proof_sha256()
            update_fields = kwargs.get("update_fields")
            if update_fields is not None and "proof_sha256" not in update_fields:
                kwargs["update_fields"] = [*update_fields, "proof_sha256"]
        super().save(*args, **kwargs)

    def compute_proof_sha256(self) -> str:
        """Hash the receipt file ('' when there is none or it can't be read)."""
        import hashlib

        if not self.proof:
            return ""
        digest = hashlib.sha256()
        try:
            f = self.proof
            closed_before = getattr(f, "closed", True)
            f.open("rb")
            f.seek(0)
            for chunk in f.chunks():
                digest.update(chunk if isinstance(chunk, bytes) else chunk.encode())
            f.seek(0)
            if closed_before and getattr(f, "_committed", True):
                f.close()
        except (OSError, ValueError):
            return ""
        return digest.hexdigest()

    @property
    def owner(self) -> User:
        return self.subscription.owner


class Feedback(models.Model):
    """
    What someone thinks of Zinwe: a 1-10 rating plus an optional note, read by the team in the admin app. From the
    owner's dashboard, or (FR-14) from a member's Zinwe window: then `owner` is the company owner (the customer it
    belongs to) and `member` / `device` say who sent it. Member feedback goes to the Zinwe team, not to the owner.
    """

    RATING_MIN = 1
    RATING_MAX = 10

    class Source(models.TextChoices):
        OWNER = "owner", "Owner dashboard"
        MEMBER = "member", "Member's Zinwe window"

    owner = models.ForeignKey(
        "z_auth.User", on_delete=models.CASCADE, related_name="feedback"
    )
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.OWNER, db_index=True)
    member = models.ForeignKey(
        "z_devices.Member", on_delete=models.SET_NULL, null=True, blank=True, related_name="feedback"
    )
    device = models.ForeignKey(
        "z_devices.Device", on_delete=models.SET_NULL, null=True, blank=True, related_name="feedback"
    )
    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(RATING_MIN), MaxValueValidator(RATING_MAX)],
        help_text="1 (poor) to 10 (excellent).",
    )
    message = models.TextField(blank=True, max_length=2000)
    page = models.CharField(max_length=120, blank=True, help_text="Dashboard page it was sent from.")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name_plural = "feedback"

    def __str__(self) -> str:
        return f"{self.owner} rated {self.rating}/10"


class CustomerRemark(models.Model):
    """The Zinwe team's internal note about a customer (never shown to the customer)."""

    owner = models.OneToOneField(
        "z_auth.User", on_delete=models.CASCADE, related_name="staff_remark"
    )
    text = models.TextField(blank=True, max_length=2000)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        "z_auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    def __str__(self) -> str:
        return f"Remark on {self.owner}"


class RenewalReminder(models.Model):
    """
    One automatic renewal email for one billing period (z_auth.renewals). The unique
    (subscription, kind, period_end) row is what stops a reminder going out twice; a renewal
    moves `ends_at`, so the next period gets its own reminders.
    """

    class Kind(models.TextChoices):
        DAYS_7 = "7d", "7 days before"
        DAYS_1 = "1d", "1 day before"
        EXPIRED = "expired", "Plan expired"

    subscription = models.ForeignKey(Subscription, on_delete=models.CASCADE, related_name="reminders")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    period_end = models.DateTimeField(help_text="The plan end date this reminder was about.")
    sent_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-sent_at"]
        constraints = [
            models.UniqueConstraint(fields=["subscription", "kind", "period_end"], name="one_reminder_per_period"),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} reminder for subscription #{self.subscription_id}"


class Announcement(models.Model):
    """
    A message the Zinwe team pushes from the admin panel: an offer, a discount, news, an update or a
    maintenance notice. Shown as a bar on the owner dashboard, on the public website (and sign-up /
    onboarding), or both, to the audience chosen, between `starts_at` and `ends_at`.
    The dashboard's own "upgrade" bar is derived from the owner's plan (z_auth.announcements), not stored.
    """

    class Kind(models.TextChoices):
        OFFER = "offer", "Offer"
        DISCOUNT = "discount", "Discount"
        NEWS = "news", "News"
        VERSION = "version", "New version"
        MAINTENANCE = "maintenance", "Maintenance"

    class Placement(models.TextChoices):
        DASHBOARD = "dashboard", "Dashboard"
        WEBSITE = "website", "Website"
        BOTH = "both", "Dashboard and website"

    class Audience(models.TextChoices):
        EVERYONE = "everyone", "Everyone"
        FREE = "free", "Owners without a paid plan"
        PAID = "paid", "Owners on a paid plan"
        PLANS = "plans", "Owners on the plans chosen"
        OWNERS = "owners", "Specific owners (by email)"

    title = models.CharField(max_length=120)
    body = models.CharField(max_length=280, blank=True)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.OFFER)
    placement = models.CharField(max_length=20, choices=Placement.choices, default=Placement.DASHBOARD)
    audience = models.CharField(
        max_length=20, choices=Audience.choices, default=Audience.EVERYONE,
        help_text="Dashboard only: the website shows every website/both message to all visitors.",
    )
    plan_codes = models.JSONField(default=list, blank=True, help_text="Plan codes, when audience is 'plans'.")
    owner_emails = models.JSONField(
        default=list, blank=True, help_text="Owners' sign-in emails, when audience is 'owners' (dashboard only)."
    )
    # An offer: the bar's button upgrades to `offer_plan` at `offer_price_inr` (before GST, for one billing cycle),
    # paid online. Each owner can use it once, while the message is live; renewals go back to the normal price.
    offer_plan = models.ForeignKey(
        "z_auth.Plan", null=True, blank=True, on_delete=models.PROTECT, related_name="+",
        help_text="Optional: the plan this message offers at a special price.",
    )
    offer_price_inr = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="The special price in rupees, before GST, for one billing cycle (at least 1.00).",
    )
    cta_label = models.CharField("button text", max_length=40, blank=True)
    cta_href = models.CharField(
        "button link", max_length=300, blank=True,
        help_text="A path on zinwe.io (e.g. /app/upgrade?plan=businesses or /plans) or an https:// link.",
    )
    starts_at = models.DateTimeField(default=timezone.now)
    ends_at = models.DateTimeField(null=True, blank=True, help_text="Empty = until switched off.")
    is_active = models.BooleanField(default=True)
    dismissible = models.BooleanField(default=True)
    priority = models.PositiveSmallIntegerField(default=0, help_text="Higher shows first.")
    created_by = models.ForeignKey(
        "z_auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-priority", "-starts_at", "-id"]
        indexes = [models.Index(fields=["is_active", "starts_at"])]

    def __str__(self) -> str:
        return f"{self.get_kind_display()}: {self.title}"


class Invoice(models.Model):
    """
    The tax invoice for one confirmed payment, frozen when staff confirm it (z_auth.invoices).

    Numbers run without gaps per Indian financial year (April-March): ZW/2026-27/0001 … — GST asks for
    a consecutive serial, so a number is only taken once a payment is confirmed. `data` holds everything
    printed (seller, customer, place of supply, lines, taxes, totals, notes) so later edits to the plan,
    company or settings never change an issued invoice.
    """

    payment = models.OneToOneField(ManualPayment, on_delete=models.PROTECT, related_name="invoice")
    number = models.CharField(max_length=20, unique=True)
    financial_year = models.CharField(max_length=7, help_text='e.g. "2026-27".')
    sequence = models.PositiveIntegerField()
    issued_at = models.DateTimeField(default=timezone.now)
    currency = models.CharField(max_length=3, choices=Subscription.Currency.choices, default=Subscription.Currency.INR)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    data = models.JSONField(default=dict)

    class Meta:
        ordering = ["-issued_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["financial_year", "sequence"], name="one_invoice_number_per_year"),
        ]

    def __str__(self) -> str:
        return self.number


class ReleaseNote(models.Model):
    """
    "What's new" (FR-21): what changed in a Zinwe release, in plain words for owners, written and published by the
    team in the admin app. Shown on the website (/whats-new) and in the dashboard once `published_at` has passed.

    `body` is plain text: a line starting "## " is a heading, "- " a bullet, a blank line ends a paragraph.
    Nothing is rendered as HTML.
    """

    version = models.CharField(max_length=20, help_text='e.g. "1.1"')
    title = models.CharField(max_length=120)
    summary = models.CharField(max_length=280, blank=True, help_text="One or two sentences for the list.")
    body = models.TextField(blank=True, max_length=20000)
    published_at = models.DateTimeField(null=True, blank=True, db_index=True, help_text="Empty = draft.")
    created_by = models.ForeignKey(
        "z_auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-published_at", "-id"]

    def __str__(self) -> str:
        return f"Zinwe {self.version}: {self.title}"

    @property
    def is_live(self) -> bool:
        return self.published_at is not None and self.published_at <= timezone.now()
