"""
Subscriptions and manual payments (no payment gateway yet).

Flow: `subscribe()` -> free plans and free trials go `active` at once; paid plans wait in
`pending_payment` -> the owner reports a payment (`submit_payment()`) -> staff
`verify_payment()` (active for one month) or `reject_payment()` (owner resubmits).

Renewal: while a paid plan is active the owner can pay for the next month at any time
(`submit_payment()` marks it `is_renewal`). Approving it extends the plan by one month from
its current end date, so no days are lost; approved after the plan lapsed, it restarts the
plan from that day. Every approved payment records the period it paid for (covers_from/until).
"""
from __future__ import annotations

import calendar
import math
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.utils import timezone

from z_analytics.services import track
from z_notification.services import notify

from .models import Company, ManualPayment, Plan, PlanActivation, Subscription, User
from .services import PlanNotAllowed, activate_plan, starts_as_trial

Status = Subscription.Status
PaymentStatus = ManualPayment.Status


class BillingError(Exception):
    """Base for billing refusals; str(exc) is user-facing."""


class CompanyRequired(BillingError):
    def __str__(self) -> str:
        return "Register your company first."


class AlreadyActive(BillingError):
    def __str__(self) -> str:
        return "You already have an active subscription. Plan changes are coming soon."


class PaymentUnderReview(BillingError):
    def __str__(self) -> str:
        return "Your payment is being reviewed. You can change plans once we've checked it."


class NothingToPay(BillingError):
    def __str__(self) -> str:
        return "There's no payment due on your subscription."


class UpgradeNotAllowed(BillingError):
    """The chosen plan isn't an upgrade from the current one."""

    def __init__(self, message: str = "Pick a plan above your current one.") -> None:
        super().__init__(message)


class ReviewNotAllowed(BillingError):
    """Admin verify/reject refused (already reviewed, subscription moved on)."""


# --- Reading ----------------------------------------------------------------


def add_months(moment: datetime, months: int = 1) -> datetime:
    """Same day-of-month `months` later, clamped to the month's last day."""
    index = moment.month - 1 + months
    year, month = moment.year + index // 12, index % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def expire_if_due(subscription: Subscription) -> Subscription:
    """Flip an `active` subscription past its `ends_at` to `expired` (and its plan activation off)."""
    if (
        subscription.status == Status.ACTIVE
        and subscription.ends_at is not None
        and subscription.ends_at <= timezone.now()
    ):
        with transaction.atomic():
            subscription.status = Status.EXPIRED
            subscription.save(update_fields=["status", "updated_at"])
            if subscription.activation_id:
                PlanActivation.objects.filter(
                    pk=subscription.activation_id, is_active=True
                ).update(is_active=False)
    return subscription


def current_subscription(user: User) -> Subscription | None:
    """The user's newest subscription (expired first if its time is up), or None."""
    subscription = (
        Subscription.objects.select_related("plan")
        .filter(owner=user)
        .order_by("-created_at", "-id")
        .first()
    )
    return expire_if_due(subscription) if subscription else None


def latest_payment(subscription: Subscription) -> ManualPayment | None:
    return subscription.payments.order_by("-submitted_at", "-id").first()


def has_payment_under_review(subscription: Subscription) -> bool:
    return subscription.payments.filter(status=PaymentStatus.SUBMITTED).exists()


def is_paid_active(subscription: Subscription) -> bool:
    """An active paid plan (not free, not a trial): the kind that can be renewed early."""
    return (
        subscription.status == Status.ACTIVE
        and not subscription.is_trial
        and subscription.amount_inr > 0
        and subscription.ends_at is not None
    )


def can_renew(subscription: Subscription) -> bool:
    """The owner may pay for the next month now (one payment under review at a time)."""
    return is_paid_active(subscription) and not has_payment_under_review(subscription)


def days_left(subscription: Subscription, now: datetime | None = None) -> int | None:
    """Whole days until an active plan ends (0 = ends today); None when open-ended or not active."""
    if subscription.status != Status.ACTIVE or subscription.ends_at is None:
        return None
    seconds = (subscription.ends_at - (now or timezone.now())).total_seconds()
    return max(0, math.ceil(seconds / 86_400))


def renewal_period(subscription: Subscription, now: datetime | None = None) -> tuple[datetime, datetime]:
    """
    The month a renewal payment would buy: from the plan's current end (or now, if that's
    already past) to one month later. Paying early never loses days.
    """
    now = now or timezone.now()
    start = subscription.ends_at if subscription.ends_at and subscription.ends_at > now else now
    return start, add_months(start, 1)


def _carry_over(subscription: Subscription, now: datetime) -> timedelta:
    """Unused paid time on the current plan, added on top when the owner upgrades (never lost)."""
    if is_paid_active(subscription) and subscription.ends_at and subscription.ends_at > now:
        return subscription.ends_at - now
    return timedelta(0)


def upgrade_period(subscription: Subscription, now: datetime | None = None) -> tuple[datetime, datetime]:
    """An upgrade starts now and runs a month, plus any paid days left on the old plan."""
    now = now or timezone.now()
    return now, add_months(now, 1) + _carry_over(subscription, now)


def next_period(payment: ManualPayment, now: datetime | None = None) -> tuple[datetime, datetime]:
    """The period approving `payment` would cover (shown to staff before they approve)."""
    now = now or timezone.now()
    subscription = payment.subscription
    if payment.upgrade_plan_id and subscription.status == Status.ACTIVE:
        return upgrade_period(subscription, now)
    if payment.is_renewal and subscription.status == Status.ACTIVE:
        return renewal_period(subscription, now)
    return now, add_months(now, 1)


def payment_instructions() -> dict[str, str]:
    """
    PaymentInstructions (portal-v1 §3.7): UPI, bank account, PayPal link, note.
    Real values come from the env (`ZINWE_PAYMENT_*`); an empty `paypal_url` hides PayPal.
    """
    return dict(settings.ZINWE_PAYMENT_INSTRUCTIONS)


# --- Owner actions ----------------------------------------------------------


@transaction.atomic
def subscribe(user: User, plan: Plan) -> Subscription:
    """
    Start a subscription to `plan`.

    - Free trial (plan has trial_days and the account hasn't used its one trial,
      see `services.starts_as_trial`): `active` at once, `is_trial`, amount 0,
      ends after trial_days. No payment step.
    - Other paid plans: `pending_payment` until staff verify a payment.
    - Free plans: `active` at once, open-ended.

    An existing `pending_payment` subscription is replaced (cancelled). Raises
    `CompanyRequired`, `AlreadyActive`, `PaymentUnderReview` or `PlanNotAllowed`
    (a legacy free trial plan whose trial was used before).
    """
    company = Company.objects.filter(owner=user).first()
    if company is None:
        raise CompanyRequired()
    current = current_subscription(user)
    if current is not None and current.status == Status.ACTIVE:
        raise AlreadyActive()
    if current is not None and current.status == Status.PENDING_PAYMENT:
        if has_payment_under_review(current):
            raise PaymentUnderReview()
        current.status = Status.CANCELLED
        current.save(update_fields=["status", "updated_at"])

    now = timezone.now()
    is_trial = starts_as_trial(user, plan)
    if plan.requires_payment and not is_trial:
        subscription = Subscription.objects.create(
            owner=user, company=company, plan=plan, status=Status.PENDING_PAYMENT,
            amount_inr=plan.price_inr_monthly, created_at=now,
        )
    else:
        activation = activate_plan(
            user, plan, source="trial" if is_trial else "subscription", trial=is_trial
        )
        subscription = Subscription.objects.create(
            owner=user, company=company, plan=plan, status=Status.ACTIVE,
            amount_inr=0 if is_trial else plan.price_inr_monthly, is_trial=is_trial,
            starts_at=activation.starts_at, ends_at=activation.ends_at,
            activation=activation, created_at=now,
        )
        transaction.on_commit(lambda: _send_subscription_active(subscription))
    transaction.on_commit(
        lambda: track(
            "subscription.created",
            {"plan": plan.code, "status": subscription.status, "is_trial": subscription.is_trial},
            actor_email=user.email,
            ref=subscription,
        )
    )
    return subscription


@transaction.atomic
def submit_payment(
    user: User, data: dict[str, Any], proof: UploadedFile | None = None
) -> ManualPayment:
    """
    Record a manual payment: for a `pending_payment` subscription (it activates the plan), for
    an active paid plan (a renewal: it extends the plan by a month once approved), or — with
    `data["upgrade_plan"]` — to move up to a higher plan from the dashboard.

    An upgrade on an active plan (free, trial or paid) keeps the current plan running until
    staff approve; then the new plan starts with any paid days left carried over. On a plan
    still waiting for its first payment, the pending choice is simply swapped for the new plan.

    Raises `NothingToPay` (nothing is due: no subscription, a free plan or a trial),
    `PaymentUnderReview` (one is already `submitted`) or `UpgradeNotAllowed`.
    """
    upgrade_plan: Plan | None = data.pop("upgrade_plan", None)
    subscription = current_subscription(user)
    if subscription is None:
        raise NothingToPay()
    if upgrade_plan is not None:
        if subscription.status == Status.PENDING_PAYMENT:
            if upgrade_plan.pk != subscription.plan_id:
                subscription = subscribe(user, upgrade_plan)  # swaps the pending choice
            upgrade_plan = None  # an ordinary first payment from here
        elif subscription.status == Status.ACTIVE:
            if not upgrade_plan.requires_payment or upgrade_plan.price_inr_monthly <= subscription.plan.price_inr_monthly:
                raise UpgradeNotAllowed()
        else:
            raise NothingToPay()
    if upgrade_plan is not None:
        renewal = False
    elif subscription.status == Status.PENDING_PAYMENT:
        renewal = False
    elif is_paid_active(subscription):
        renewal = True
    else:
        raise NothingToPay()
    subscription = Subscription.objects.select_for_update().get(pk=subscription.pk)
    if has_payment_under_review(subscription):
        raise PaymentUnderReview()
    payment = ManualPayment(subscription=subscription, is_renewal=renewal, upgrade_plan=upgrade_plan, **data)
    if proof is not None:
        payment.proof = proof
    payment.save()
    transaction.on_commit(lambda: _after_payment_submitted(payment))
    return payment


def _after_payment_submitted(payment: ManualPayment) -> None:
    subscription = payment.subscription
    owner = subscription.owner
    track(
        "payment.submitted",
        {"method": payment.method, "amount_inr": str(payment.amount_inr), "plan": subscription.plan.code},
        actor_email=owner.email,
        ref=payment,
    )
    notify(
        "email",
        settings.ZINWE_TEAM_NOTIFY_EMAIL,
        "payment_received_team",
        {
            "payment": payment,
            "subscription": subscription,
            "owner": owner,
            "company": subscription.company,
            "plan": subscription.plan,
        },
        ref=payment,
    )


# --- Staff review (Django admin) --------------------------------------------


@transaction.atomic
def verify_payment(payment_id: int, reviewer: User | None) -> ManualPayment:
    """
    Accept a submitted payment, owner emailed. Raises `ReviewNotAllowed`.

    - New plan (`pending_payment`): active for one month from now, PlanActivation created.
    - Renewal of an active plan: extended by one month from its current end date.
    - Renewal approved after the plan lapsed (`expired`): restarted for one month from now.
    - Upgrade: the subscription switches to the new plan now, for a month plus any paid days
      left on the old one (or a plain month if the old plan lapsed meanwhile).
    The payment records the period it paid for (covers_from / covers_until).
    """
    payment = ManualPayment.objects.select_for_update().get(pk=payment_id)
    if payment.status != PaymentStatus.SUBMITTED:
        raise ReviewNotAllowed(f"Payment #{payment.pk} is already {payment.get_status_display().lower()}.")
    subscription = Subscription.objects.select_for_update().select_related("plan", "owner").get(
        pk=payment.subscription_id
    )
    now = timezone.now()
    extending = payment.is_renewal and subscription.status == Status.ACTIVE
    if payment.upgrade_plan_id and subscription.status in (Status.ACTIVE, Status.EXPIRED):
        new_plan = payment.upgrade_plan
        covers_from, ends_at = (
            upgrade_period(subscription, now) if subscription.status == Status.ACTIVE else (now, add_months(now, 1))
        )
        try:
            activation = activate_plan(
                subscription.owner, new_plan, source="upgrade", allow_paid=True, ends_at=ends_at,
            )
        except PlanNotAllowed as exc:
            raise ReviewNotAllowed(f"Payment #{payment.pk}: {exc}") from exc
        subscription.plan = new_plan
        subscription.amount_inr = new_plan.price_inr_monthly
        subscription.is_trial = False
        subscription.status = Status.ACTIVE
        subscription.starts_at = now
        subscription.ends_at = ends_at
        subscription.activation = activation
        subscription.save(update_fields=[
            "plan", "amount_inr", "is_trial", "status", "starts_at", "ends_at", "activation", "updated_at",
        ])
    elif extending:
        covers_from, ends_at = renewal_period(subscription, now)
        if subscription.activation_id:
            PlanActivation.objects.filter(pk=subscription.activation_id).update(ends_at=ends_at, is_active=True)
        subscription.ends_at = ends_at
        subscription.save(update_fields=["ends_at", "updated_at"])
    elif subscription.status == Status.PENDING_PAYMENT or (
        payment.is_renewal and subscription.status == Status.EXPIRED
    ):
        covers_from, ends_at = now, add_months(now, 1)
        try:
            activation = activate_plan(
                subscription.owner, subscription.plan, source="manual-payment",
                allow_paid=True, ends_at=ends_at,
            )
        except PlanNotAllowed as exc:
            raise ReviewNotAllowed(f"Payment #{payment.pk}: {exc}") from exc
        subscription.status = Status.ACTIVE
        subscription.starts_at = now
        subscription.ends_at = ends_at
        subscription.activation = activation
        subscription.save(update_fields=["status", "starts_at", "ends_at", "activation", "updated_at"])
    else:
        raise ReviewNotAllowed(
            f"Payment #{payment.pk}: its subscription is {subscription.get_status_display().lower()}, "
            "not waiting for payment."
        )
    payment.status = PaymentStatus.VERIFIED
    payment.reviewed_at = now
    payment.reviewed_by = reviewer
    payment.rejection_reason = ""
    payment.covers_from = covers_from
    payment.covers_until = ends_at
    payment.save(update_fields=[
        "status", "reviewed_at", "reviewed_by", "rejection_reason", "covers_from", "covers_until",
    ])
    transaction.on_commit(lambda: _after_payment_verified(payment, subscription))
    return payment


def _after_payment_verified(payment: ManualPayment, subscription: Subscription) -> None:
    track(
        "payment.verified",
        {"amount_inr": str(payment.amount_inr), "plan": subscription.plan.code, "renewal": payment.is_renewal},
        actor_email=subscription.owner.email,
        ref=payment,
    )
    _send_subscription_active(subscription, renewed=payment.is_renewal)


@transaction.atomic
def reject_payment(payment_id: int, reviewer: User | None, reason: str) -> ManualPayment:
    """
    Turn down a submitted payment with a reason (shown to the owner).
    The subscription stays `pending_payment`, so the owner can resubmit.
    Raises `ReviewNotAllowed`.
    """
    reason = (reason or "").strip()
    if not reason:
        raise ReviewNotAllowed("Give a reason the owner will understand.")
    payment = ManualPayment.objects.select_for_update().select_related(
        "subscription__owner", "subscription__plan"
    ).get(pk=payment_id)
    if payment.status != PaymentStatus.SUBMITTED:
        raise ReviewNotAllowed(f"Payment #{payment.pk} is already {payment.get_status_display().lower()}.")
    payment.status = PaymentStatus.REJECTED
    payment.rejection_reason = reason
    payment.reviewed_at = timezone.now()
    payment.reviewed_by = reviewer
    payment.save(update_fields=["status", "rejection_reason", "reviewed_at", "reviewed_by"])
    transaction.on_commit(lambda: _after_payment_rejected(payment))
    return payment


def _after_payment_rejected(payment: ManualPayment) -> None:
    owner = payment.subscription.owner
    track("payment.rejected", {}, actor_email=owner.email, ref=payment)
    notify(
        "email",
        owner.email,
        "payment_rejected",
        {
            "first_name": owner.get_short_name() if owner.full_name else "there",
            "payment": payment,
            "plan": payment.subscription.plan,
            "reason": payment.rejection_reason,
        },
        ref=payment,
    )


def trial_length(days: int) -> str:
    """90 -> "3-month", 14 -> "14-day" (for "Your free 3-month trial ...")."""
    if days and days % 30 == 0:
        return f"{days // 30}-month"
    return f"{days}-day"


def _send_subscription_active(subscription: Subscription, renewed: bool = False) -> None:
    owner = subscription.owner
    notify(
        "email",
        owner.email,
        "subscription_active",
        {
            "first_name": owner.get_short_name() if owner.full_name else "there",
            "plan": subscription.plan,
            "subscription": subscription,
            "is_trial": subscription.is_trial,
            "is_renewal": renewed,
            "trial_length": trial_length(subscription.plan.trial_days),
        },
        ref=subscription,
    )
