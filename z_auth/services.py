"""
Business logic for z_auth accounts: early access, sign-up / sign-in, plans.
Company and billing live in `company.py` / `billing.py`. Views stay thin.

Side effects (analytics + notifications) are explicit calls scheduled with
`transaction.on_commit`, so they only fire once the row is really saved.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.contrib.auth import authenticate, password_validation
from django.contrib.auth.models import update_last_login
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.authtoken.models import Token

from z_analytics.services import track
from z_main.client import get_client_ip
from z_notification.services import notify

from . import otp
from .models import EarlyAccessRegistration as EAR
from .models import OneTimeCode, Plan, PlanActivation, Subscription, User

PLATFORM_LABELS = {"windows": "Windows", "linux": "Linux", "macos": "macOS"}
CONCERN_LABELS = {
    "usb": "USB / removable drives",
    "remote": "Remote access",
    "installs": "Unapproved software installs",
    "email": "Email data leaks",
    "compliance": "Compliance",
}


def _human_list(items: list[str]) -> str:
    """["a", "b", "c"] -> "a, b and c"."""
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


# --- Early access -----------------------------------------------------------


def register_early_access(
    data: dict[str, Any], ip: str | None, user_agent: str
) -> tuple[EAR, bool]:
    """
    Create an early-access registration, or return the existing one.

    `data` is validated, normalised serializer output (email already lowercased).
    Returns (registration, created). Duplicates (case-insensitive email) never
    create a row and never re-send the confirmation email.
    """
    email = data["email"].strip().lower()

    existing = EAR.objects.filter(email__iexact=email).first()
    if existing is not None:
        _schedule_duplicate_tracking(existing)
        return existing, False

    plan = Plan.objects.filter(code=Plan.EARLY_ACCESS).first()
    trial_days = (plan.trial_days if plan and plan.trial_days else None) or getattr(
        settings, "ZINWE_EARLY_ACCESS_TRIAL_DAYS", 0
    )
    try:
        with transaction.atomic():
            registration = EAR.objects.create(
                name=data["name"],
                email=email,
                company=data["company"],
                phone=data.get("phone") or "",
                team_size=data["team_size"],
                platforms=list(data.get("platforms") or []),
                concerns=list(data.get("concerns") or []),
                plan=plan,
                trial_days=trial_days,
                ip=ip,
                user_agent=(user_agent or "")[:255],
            )
    except IntegrityError:
        # Lost a race with a concurrent submission of the same email.
        existing = EAR.objects.get(email=email)
        _schedule_duplicate_tracking(existing)
        return existing, False

    transaction.on_commit(lambda: _after_early_access_created(registration))
    return registration, True


def _schedule_duplicate_tracking(registration: EAR) -> None:
    transaction.on_commit(
        lambda: track(
            "early_access.duplicate",
            {"registration_id": registration.id},
            actor_email=registration.email,
            ref=registration,
        )
    )


def _after_early_access_created(registration: EAR) -> None:
    """Post-commit side effects for a brand-new registration. Never raises."""
    track(
        "early_access.registered",
        {
            "team_size": registration.team_size,
            "platforms": registration.platforms,
            "concerns": registration.concerns,
        },
        actor_email=registration.email,
        ref=registration,
    )

    platforms_text = _human_list([PLATFORM_LABELS.get(p, p) for p in registration.platforms])
    concerns_text = ", ".join(CONCERN_LABELS.get(c, c) for c in registration.concerns)
    context = {
        "registration": registration,
        "first_name": registration.name.split()[0] if registration.name.split() else "there",
        "company": registration.company,
        "trial_days": registration.trial_days,
        "platforms_text": f"{platforms_text} computers" if platforms_text else "",
        "concerns_text": concerns_text,
    }
    notify("email", registration.email, "early_access_confirmation", context, ref=registration)
    notify(
        "email",
        settings.ZINWE_TEAM_NOTIFY_EMAIL,
        "early_access_team_alert",
        {**context, "platforms_text": platforms_text},
        ref=registration,
    )


# --- Accounts: sign-up, sign-in, password reset -------------------------------


class InvalidCode(Exception):
    """A one-time code didn't check out (wrong, expired, burnt or no account)."""

    message = "That code is incorrect or has expired."

    def __str__(self) -> str:
        return self.message


class InvalidCredentials(Exception):
    message = "Email or password is incorrect."

    def __str__(self) -> str:
        return self.message


class EmailNotVerified(Exception):
    """Right password, but the account never entered its sign-up code."""

    code = "email_not_verified"
    message = "Verify your email to continue. Enter the code we emailed you."

    def __str__(self) -> str:
        return self.message


class PasswordRejected(Exception):
    """The new password failed the validators; `messages` are user-facing."""

    def __init__(self, messages: list[str]) -> None:
        super().__init__(" ".join(messages))
        self.messages = messages


CODE_TEMPLATES = {
    OneTimeCode.Purpose.SIGNUP: "otp_signup",
    OneTimeCode.Purpose.LOGIN: "otp_login",
    OneTimeCode.Purpose.PASSWORD_RESET: "otp_password_reset",
}


def _first_name(user: User | None) -> str:
    if user is not None and user.full_name.split():
        return user.full_name.split()[0]
    return "there"


def send_code_email(email: str, purpose: str, code: str, first_name: str = "there") -> None:
    """Email a one-time code. The logged Notification has the code masked."""
    notify(
        "email",
        email,
        CODE_TEMPLATES[purpose],
        {
            "code": code,
            "first_name": first_name,
            "minutes": int(otp.CODE_TTL.total_seconds() // 60),
        },
        ref=("z_auth.onetimecode", purpose),
        redact=[code],
    )


def _issue_and_send(email: str, purpose: str, ip: str | None, recipient: User | None) -> None:
    """
    Count a code request and, if there is an eligible `recipient`, email the code.

    The code row is written whether or not anyone receives it, so cooldowns
    and hourly caps can't reveal whether an account exists. Raises
    `otp.OtpThrottled`.
    """
    code = otp.issue_code(email, purpose, ip=ip)
    if recipient is not None:
        first_name = _first_name(recipient)
        transaction.on_commit(lambda: send_code_email(email, purpose, code, first_name))


def _code_recipient(email: str, purpose: str) -> User | None:
    """The account allowed to receive a code for this purpose, if any."""
    user = User.objects.filter(email__iexact=otp.normalise_email(email)).first()
    if user is None:
        return None
    if purpose == OneTimeCode.Purpose.SIGNUP:
        return user if user.is_pending_verification else None
    return user if user.is_active else None


@transaction.atomic
def accept_terms(user: User) -> User:
    """Record that the owner accepted the current Terms (the dashboard's updated-terms prompt)."""
    user.terms_accepted_at = timezone.now()
    user.terms_version = settings.ZINWE_LEGAL_VERSION
    user.save(update_fields=["terms_accepted_at", "terms_version"])
    return user


@transaction.atomic  # select_for_update below: PostgreSQL refuses it outside a transaction (SQLite ignores it)
def signup(
    full_name: str, email: str, password: str, ip: str | None, accepted_terms: bool = False
) -> tuple[User, bool, int]:
    """
    Create an unverified account (or refresh an unverified one) and send a code.

    Input is validated (an existing *active* account is rejected by the
    serializer). Returns (user, otp_sent, resend_in_seconds). Inside the resend
    cooldown no new code is sent: the earlier one still works.
    """
    email = otp.normalise_email(email)
    terms = (
        {"terms_accepted_at": timezone.now(), "terms_version": settings.ZINWE_LEGAL_VERSION} if accepted_terms else {}
    )
    user = User.objects.select_for_update().filter(email__iexact=email).first()
    if user is not None:
        user.full_name = full_name
        user.set_password(password)
        for field, value in terms.items():
            setattr(user, field, value)
        user.save(update_fields=["full_name", "password", *terms.keys()])
    else:
        user = User.objects.create_user(
            email=email, password=password, full_name=full_name, is_active=False, **terms
        )
    transaction.on_commit(lambda: track("auth.signup_started", {}, actor_email=email, ref=user))
    try:
        _issue_and_send(email, OneTimeCode.Purpose.SIGNUP, ip, user)
    except otp.OtpThrottled as exc:
        return user, False, exc.retry_after
    return user, True, otp.RESEND_COOLDOWN_SECONDS


def verify_signup(email: str, code: str) -> User:
    """Activate an account with its sign-up code. Raises `InvalidCode`."""
    if not otp.verify_code(email, OneTimeCode.Purpose.SIGNUP, code):
        raise InvalidCode()
    with transaction.atomic():
        user = (
            User.objects.select_for_update()
            .filter(email__iexact=otp.normalise_email(email))
            .first()
        )
        if user is None or not user.is_pending_verification:
            raise InvalidCode()
        user.is_active = True
        user.email_verified_at = timezone.now()
        user.save(update_fields=["is_active", "email_verified_at"])
        update_last_login(None, user)
        transaction.on_commit(lambda: _after_signup_verified(user))
    return user


def _after_signup_verified(user: User) -> None:
    track("auth.signup_verified", {}, actor_email=user.email, ref=user)
    notify("email", user.email, "welcome", {"first_name": _first_name(user), "user": user}, ref=user)


def resend_code(email: str, purpose: str, ip: str | None) -> None:
    """Request a code for any purpose. Silent for ineligible addresses. Raises `OtpThrottled`."""
    with transaction.atomic():
        _issue_and_send(email, purpose, ip, _code_recipient(email, purpose))


def password_login(request: Any, email: str, password: str) -> User:
    """
    Email + password sign-in.

    Raises `InvalidCredentials`, or `EmailNotVerified` when the password is
    right but the account is still unverified (a fresh sign-up code is sent,
    unless the cooldown / hourly cap says not yet).
    """
    email = otp.normalise_email(email)
    user = authenticate(request, email=email, password=password)
    if user is not None:
        update_last_login(None, user)
        track("auth.login", {"method": "password"}, actor_email=email, ref=user)
        return user
    pending = User.objects.filter(email__iexact=email).first()
    if pending is not None and pending.is_pending_verification and pending.check_password(password):
        try:
            with transaction.atomic():
                _issue_and_send(email, OneTimeCode.Purpose.SIGNUP, get_client_ip(request), pending)
        except otp.OtpThrottled:
            pass  # the previous code is still valid
        raise EmailNotVerified()
    raise InvalidCredentials()


def request_login_code(email: str, ip: str | None) -> None:
    """Email a sign-in code if an active account exists. Raises `OtpThrottled`."""
    resend_code(email, OneTimeCode.Purpose.LOGIN, ip)


def login_with_code(email: str, code: str) -> User:
    """Sign in with an emailed code. Raises `InvalidCode`."""
    if not otp.verify_code(email, OneTimeCode.Purpose.LOGIN, code):
        raise InvalidCode()
    user = User.objects.filter(email__iexact=otp.normalise_email(email)).first()
    if user is None or not user.is_active:
        raise InvalidCode()
    _mark_email_verified(user)
    update_last_login(None, user)
    track("auth.login", {"method": "otp"}, actor_email=user.email, ref=user)
    return user


def request_password_reset(email: str, ip: str | None) -> None:
    """Email a reset code if an active account exists. Raises `OtpThrottled`."""
    resend_code(email, OneTimeCode.Purpose.PASSWORD_RESET, ip)


def reset_password(email: str, code: str, new_password: str) -> User:
    """
    Set a new password with a reset code and revoke every API token.

    The password is checked first, so a weak password doesn't burn the code.
    Raises `PasswordRejected` or `InvalidCode`.
    """
    email = otp.normalise_email(email)
    user = User.objects.filter(email__iexact=email).first()
    try:
        password_validation.validate_password(new_password, user=user or User(email=email))
    except DjangoValidationError as exc:
        raise PasswordRejected(list(exc.messages)) from exc
    if not otp.verify_code(email, OneTimeCode.Purpose.PASSWORD_RESET, code):
        raise InvalidCode()
    if user is None or not user.is_active:
        raise InvalidCode()
    with transaction.atomic():
        user.set_password(new_password)
        user.save(update_fields=["password"])
        _mark_email_verified(user)
        Token.objects.filter(user=user).delete()
        update_last_login(None, user)
        transaction.on_commit(lambda: track("auth.password_reset", {}, actor_email=user.email, ref=user))
    return user


class WrongPassword(Exception):
    """The current password given to `change_password` doesn't match."""


def change_password(user: User, current_password: str, new_password: str) -> User:
    """
    Signed-in password change: check the current password, set the new one and
    rotate the API token, which signs out every other device (the caller gets
    the new token back). Raises `WrongPassword` or `PasswordRejected`.
    """
    if not user.check_password(current_password):
        raise WrongPassword()
    try:
        password_validation.validate_password(new_password, user=user)
    except DjangoValidationError as exc:
        raise PasswordRejected(list(exc.messages)) from exc
    if current_password == new_password:
        raise PasswordRejected(["Choose a password different from your current one."])
    with transaction.atomic():
        user.set_password(new_password)
        user.save(update_fields=["password"])
        Token.objects.filter(user=user).delete()
        transaction.on_commit(lambda: track("auth.password_changed", {}, actor_email=user.email, ref=user))
    return user


def _mark_email_verified(user: User) -> None:
    """A code that reached the inbox proves the address."""
    if user.email_verified_at is None:
        user.email_verified_at = timezone.now()
        user.save(update_fields=["email_verified_at"])


def get_token(user: User) -> str:
    """The user's API token (created if missing)."""
    token, _ = Token.objects.get_or_create(user=user)
    return token.key


def logout(user: User, token: Any) -> None:
    """Delete the token used for this request (or all the user's tokens for session auth)."""
    if isinstance(token, Token):
        token.delete()
    else:
        Token.objects.filter(user=user).delete()


def register_user(
    email: str, password: str, full_name: str, company: str = "", phone: str = ""
) -> User:
    """
    Create an active, verified user. Input must already be validated.

    Deprecated together with `POST register/`: new accounts use `signup()` (email code).
    """
    return User.objects.create_user(
        email=email, password=password, full_name=full_name, company=company, phone=phone,
        email_verified_at=timezone.now(),
    )


# --- Plans ------------------------------------------------------------------


class PlanNotAllowed(Exception):
    """Raised when a user may not activate a plan; str(exc) is user-facing."""


# --- Free trials: one per account, ever ----------------------------------------
#
# A trial is a plan's `trial_days` given away without payment. Since v1.1 the
# trial lives on a paid plan (Businesses: 90 days); the legacy free
# `early-access` plan was the same deal, so having used it counts.


def trial_used(user: User) -> bool:
    """True once the account has had any free trial (an `is_trial` subscription or legacy early access)."""
    return (
        Subscription.objects.filter(owner=user, is_trial=True).exists()
        or PlanActivation.objects.filter(user=user, plan__code=Plan.EARLY_ACCESS).exists()
    )


def trial_available(plan: Plan, user: User | None = None) -> bool:
    """Whether choosing `plan` would start a free trial. Anonymous callers: yes, if the plan has one."""
    if not plan.trial_days:
        return False
    if user is None or not user.is_authenticated:
        return True
    return not trial_used(user)


def starts_as_trial(user: User, plan: Plan) -> bool:
    """
    Should activating `plan` now be a free trial?

    Paid plans with trial_days: only while the account's trial is unused (otherwise
    the owner pays). Free plans with trial_days (legacy early access) are always a
    trial, so `activate_plan` refuses them once the trial is used.
    """
    if not plan.trial_days:
        return False
    if plan.requires_payment:
        return not trial_used(user)
    return True


@transaction.atomic
def activate_plan(
    user: User,
    plan: Plan,
    source: str = "self-serve",
    *,
    trial: bool = False,
    allow_paid: bool = False,
    ends_at: datetime | None = None,
) -> PlanActivation:
    """
    Make `plan` the user's single active plan.

    Any previously active activation is deactivated. A trial (`trial=True`)
    ends after plan.trial_days; anything else is open-ended unless `ends_at`
    is given (e.g. a paid month).

    Rules:
    - A trial is once per account, ever (`trial_used`), whichever plan it was on.
      A later *paid* activation of the same plan is fine.
    - Paid plans need a verified payment (billing passes `allow_paid=True`) or a trial.
    - Starting a trial marks the owner's early-access waitlist registration (if
      any) as activated; no registration is needed.
    """
    if trial:
        if not plan.trial_days:
            raise PlanNotAllowed("This plan has no free trial.")
        if trial_used(user):
            raise PlanNotAllowed("You've already used this trial.")
    elif plan.requires_payment and not allow_paid:
        raise PlanNotAllowed("This plan needs a payment. Choose it on the subscription page.")

    registration = None
    if trial:
        registration = EAR.objects.select_for_update().filter(email=user.email.lower()).first()

    now = timezone.now()
    PlanActivation.objects.select_for_update().filter(user=user, is_active=True).update(
        is_active=False
    )
    if trial:
        ends_at = now + timedelta(days=plan.trial_days)
    activation = PlanActivation.objects.create(
        user=user, plan=plan, starts_at=now, ends_at=ends_at, is_active=True, source=source
    )
    if registration is not None and registration.status != EAR.Status.ACTIVATED:
        registration.status = EAR.Status.ACTIVATED
        registration.save(update_fields=["status", "updated_at"])
    transaction.on_commit(
        lambda: track(
            "plan.activated",
            {"plan": plan.code, "source": source},
            actor_email=user.email,
            ref=activation,
        )
    )
    return activation
