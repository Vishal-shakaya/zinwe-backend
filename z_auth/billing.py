"""
Subscriptions and manual payments (no payment gateway yet).

Flow: `subscribe()` -> free plans and free trials go `active` at once; paid plans wait in
`pending_payment` -> the owner reports a payment (`submit_payment()`) -> staff
`verify_payment()` (active for one month) or `reject_payment()` (owner resubmits).
"""
from __future__ import annotations

import calendar
from datetime import datetime
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
    Record a manual payment against the pending subscription.

    Raises `NothingToPay` (no `pending_payment` subscription) or
    `PaymentUnderReview` (one is already `submitted`).
    """
    subscription = current_subscription(user)
    if subscription is None or subscription.status != Status.PENDING_PAYMENT:
        raise NothingToPay()
    subscription = Subscription.objects.select_for_update().get(pk=subscription.pk)
    if has_payment_under_review(subscription):
        raise PaymentUnderReview()
    payment = ManualPayment(subscription=subscription, **data)
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
    Accept a submitted payment: subscription active for one month from now,
    PlanActivation created (one active per user), owner emailed.
    Raises `ReviewNotAllowed`.
    """
    payment = ManualPayment.objects.select_for_update().get(pk=payment_id)
    if payment.status != PaymentStatus.SUBMITTED:
        raise ReviewNotAllowed(f"Payment #{payment.pk} is already {payment.get_status_display().lower()}.")
    subscription = Subscription.objects.select_for_update().select_related("plan", "owner").get(
        pk=payment.subscription_id
    )
    if subscription.status != Status.PENDING_PAYMENT:
        raise ReviewNotAllowed(
            f"Payment #{payment.pk}: its subscription is {subscription.get_status_display().lower()}, "
            "not waiting for payment."
        )
    now = timezone.now()
    ends_at = add_months(now, 1)
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
    payment.status = PaymentStatus.VERIFIED
    payment.reviewed_at = now
    payment.reviewed_by = reviewer
    payment.rejection_reason = ""
    payment.save(update_fields=["status", "reviewed_at", "reviewed_by", "rejection_reason"])
    transaction.on_commit(lambda: _after_payment_verified(payment, subscription))
    return payment


def _after_payment_verified(payment: ManualPayment, subscription: Subscription) -> None:
    track(
        "payment.verified",
        {"amount_inr": str(payment.amount_inr), "plan": subscription.plan.code},
        actor_email=subscription.owner.email,
        ref=payment,
    )
    _send_subscription_active(subscription)


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


def _send_subscription_active(subscription: Subscription) -> None:
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
            "trial_length": trial_length(subscription.plan.trial_days),
        },
        ref=subscription,
    )
