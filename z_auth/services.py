"""
Business logic for z_auth. Views stay thin and call into here.

Side effects (analytics + notifications) are explicit calls scheduled with
`transaction.on_commit`, so they only fire once the row is really saved.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from z_analytics.services import track
from z_notification.services import notify

from .models import EarlyAccessRegistration as EAR
from .models import Plan, PlanActivation, User

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
        settings, "ZINWE_EARLY_ACCESS_TRIAL_DAYS", 90
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


# --- Accounts ---------------------------------------------------------------


def register_user(
    email: str, password: str, full_name: str, company: str = "", phone: str = ""
) -> User:
    """Create a regular user. Input must already be validated."""
    return User.objects.create_user(
        email=email, password=password, full_name=full_name, company=company, phone=phone
    )


# --- Plans ------------------------------------------------------------------


class PlanNotAllowed(Exception):
    """Raised when a user may not activate a plan; str(exc) is user-facing."""


@transaction.atomic
def activate_plan(user: User, plan: Plan, source: str = "self-serve") -> PlanActivation:
    """
    Make `plan` the user's single active plan.

    Any previously active activation is deactivated. Trial plans
    (trial_days > 0) get ends_at = now + trial_days; others are open-ended.

    Rules:
    - A trial plan can be activated once per user (no restarting the clock).
    - The early-access plan needs an early-access registration for the
      user's email; activating it marks that registration as activated.
    """
    registration = None
    if plan.code == Plan.EARLY_ACCESS:
        registration = EAR.objects.select_for_update().filter(email=user.email.lower()).first()
        if registration is None:
            raise PlanNotAllowed("This plan is only for early-access sign-ups.")
    if plan.trial_days and PlanActivation.objects.filter(user=user, plan=plan).exists():
        raise PlanNotAllowed("You've already used this trial.")

    now = timezone.now()
    PlanActivation.objects.select_for_update().filter(user=user, is_active=True).update(
        is_active=False
    )
    ends_at = now + timedelta(days=plan.trial_days) if plan.trial_days else None
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
