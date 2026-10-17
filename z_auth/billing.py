"""
Subscriptions and manual payments (no payment gateway yet).

Flow: `subscribe()` -> free plans and free trials go `active` at once; paid plans wait in
`pending_payment` -> the owner reports a payment (`submit_payment()`) -> staff
`verify_payment()` (active for one month) or `reject_payment()` (owner resubmits).

Country decides the money (z_auth.pricing): India = INR monthly + GST; elsewhere = USD yearly, no GST
(export under LUT). The subscription keeps its currency and cycle; each payment freezes the quote it
was made against (`ManualPayment.due`), and confirming it issues a numbered invoice (z_auth.invoices).

Renewal: while a paid plan is active the owner can pay for the next period at any time
(`submit_payment()` marks it `is_renewal`). Approving it extends the plan by one month from
its current end date, so no days are lost; approved after the plan lapsed, it restarts the
plan from that day. Every approved payment records the period it paid for (covers_from/until).
"""
from __future__ import annotations

import calendar
import logging
import math
import os
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.utils import timezone

from z_analytics.services import track
from z_notification.services import notify

from . import pricing
from .models import Company, ManualPayment, Plan, PlanActivation, Subscription, User
from .services import PlanNotAllowed, activate_plan, starts_as_trial

Status = Subscription.Status
PaymentStatus = ManualPayment.Status
logger = logging.getLogger("zinwe.billing")


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


def cycle_months(subscription: Subscription) -> int:
    """1 for a monthly subscription, 12 for a yearly one (customers outside India)."""
    return pricing.months_in(subscription.billing_cycle)


def renewal_period(subscription: Subscription, now: datetime | None = None) -> tuple[datetime, datetime]:
    """
    The period a renewal payment would buy: from the plan's current end (or now, if that's
    already past) to one billing cycle later. Paying early never loses days.
    """
    now = now or timezone.now()
    start = subscription.ends_at if subscription.ends_at and subscription.ends_at > now else now
    return start, add_months(start, cycle_months(subscription))


def _carry_over(subscription: Subscription, now: datetime) -> timedelta:
    """Unused paid time on the current plan, added on top when the owner upgrades (never lost)."""
    if is_paid_active(subscription) and subscription.ends_at and subscription.ends_at > now:
        return subscription.ends_at - now
    return timedelta(0)


def upgrade_period(subscription: Subscription, now: datetime | None = None) -> tuple[datetime, datetime]:
    """An upgrade starts now and runs one billing cycle, plus any paid days left on the old plan."""
    now = now or timezone.now()
    return now, add_months(now, cycle_months(subscription)) + _carry_over(subscription, now)


def next_period(payment: ManualPayment, now: datetime | None = None) -> tuple[datetime, datetime]:
    """The period approving `payment` would cover (shown to staff before they approve)."""
    now = now or timezone.now()
    subscription = payment.subscription
    if payment.upgrade_plan_id and subscription.status == Status.ACTIVE:
        return upgrade_period(subscription, now)
    if payment.is_renewal and subscription.status == Status.ACTIVE:
        return renewal_period(subscription, now)
    return now, add_months(now, cycle_months(subscription))


def invoice_for(payment: ManualPayment) -> dict[str, Any]:
    """The frozen tax invoice of a confirmed payment (z_auth.invoices). The caller checks it's confirmed."""
    from .invoices import invoice_for_payment

    return invoice_for_payment(payment).data


def amount_due(subscription: Subscription, upgrade_plan: Plan | None = None) -> pricing.Quote:
    """What the owner pays next: the subscription's plan (or `upgrade_plan`) for one billing cycle, with tax."""
    return pricing.quote_for_subscription(subscription, upgrade_plan)


def upi_uri(upi_id: str, payee: str, amount: str, note: str) -> str:
    """The `upi://pay?...` link with the exact amount: opens GPay / PhonePe / Paytm filled in (J3-07)."""
    from urllib.parse import quote as q

    return f"upi://pay?pa={q(upi_id)}&pn={q(payee)}&am={amount}&cu=INR&tn={q(note[:40])}"


def _upi_qr_svg(upi_id: str, payee: str, amount: str, note: str) -> str:
    """A UPI QR (any UPI app) with the exact amount filled in, as an inline SVG."""
    import segno

    uri = upi_uri(upi_id, payee, amount, note)
    return segno.make(uri, error="m").svg_inline(scale=4, border=2, dark="#0d0d0e")


def upi_note(company: Company | None, plan_name: str) -> str:
    """The UPI payment note: the reference the screens ask for (the company name), else "Zinwe <plan>"."""
    return (company.name if company and company.name else f"Zinwe {plan_name}")[:40]


def _upi_for(quote: pricing.Quote, company: Company | None = None) -> tuple[str, str]:
    """(QR svg, upi:// link) for an INR quote when a UPI ID is configured; ("", "") otherwise."""
    upi_id = settings.ZINWE_PAYMENT_INSTRUCTIONS.get("upi_id", "")
    if not upi_id or quote.currency != pricing.INR or quote.total <= 0:
        return "", ""
    payee = settings.ZINWE_PAYMENT_INSTRUCTIONS.get("payee_name", "")
    amount, note = f"{quote.total:.2f}", upi_note(company, quote.plan_name)
    return _upi_qr_svg(upi_id, payee, amount, note), upi_uri(upi_id, payee, amount, note)


def payment_instructions(subscription: Subscription | None = None, company: Company | None = None) -> dict[str, Any]:
    """
    How to pay (portal-v1 §3.7, extended 2026-10-15; every way shown since 2026-10-16):

    - UPI (with a QR code holding the exact amount due, for INR quotes) and bank transfer (NEFT / IMPS / RTGS).
    - International: the customer's own Skydo payment link (card or US bank, set in the admin app), else the
      general Skydo page (`ZINWE_SKYDO_PAYMENT_URL`), and the Skydo US account for ACH / wire.
    - PayPal only as a fallback for customers outside India while no Skydo account is set up.

    `region` ("IN" / "INTL", from the subscription's currency) only picks which way is shown first.
    Real values come from the env (`ZINWE_PAYMENT_*`, `ZINWE_SKYDO_*`); empty ones are hidden.
    """
    data: dict[str, Any] = dict(settings.ZINWE_PAYMENT_INSTRUCTIONS)
    company = company or (subscription.company if subscription else None)
    currency = subscription.currency if subscription else pricing.currency_for(company)
    data["region"] = "IN" if currency == pricing.INR else "INTL"
    data["currency"] = currency
    skydo = {k: v for k, v in settings.ZINWE_SKYDO_ACCOUNT.items() if v}
    data["skydo"] = skydo if skydo.get("account_number") else None
    data["payment_link"] = (subscription.payment_link if subscription else "") or settings.ZINWE_SKYDO_PAYMENT_URL
    data["upi_qr_svg"] = ""
    data["upi_uri"] = ""
    if data["region"] == "INTL":
        if data["skydo"] or data["payment_link"]:
            data["paypal_url"] = ""
        data["note"] = "Use your company name and quote number as the payment reference."
    else:
        data["paypal_url"] = ""
        if subscription is not None and data.get("upi_id") and subscription.status != Status.CANCELLED:
            due = amount_due(subscription)
            if due.total > 0:
                note = upi_note(company, due.plan_name)
                data["upi_qr_svg"] = _upi_qr_svg(data["upi_id"], data.get("payee_name", ""), f"{due.total:.2f}", note)
                data["upi_uri"] = upi_uri(data["upi_id"], data.get("payee_name", ""), f"{due.total:.2f}", note)
    return data


def check_upgrade(subscription: Subscription | None, plan: Plan) -> Subscription:
    """
    The subscription an upgrade to `plan` would start from: an active plan (free, trial or paid), no payment
    under review, and `plan` a paid plan that costs more. Raises `UpgradeNotAllowed` / `PaymentUnderReview`.
    """
    if subscription is None or subscription.status != Status.ACTIVE:
        raise UpgradeNotAllowed("An upgrade starts from an active plan. Choose a plan on the plans page instead.")
    if has_payment_under_review(subscription):
        raise PaymentUnderReview()
    if not plan.is_active or not plan.requires_payment or plan.price_inr_monthly <= subscription.plan.price_inr_monthly:
        raise UpgradeNotAllowed()
    return subscription


def carried_days(subscription: Subscription, now: datetime | None = None) -> int:
    """Whole paid days left on the current plan that an upgrade adds on top (never lost)."""
    return _carry_over(subscription, now or timezone.now()).days


def upgrade_preview(user: User, plan: Plan, now: datetime | None = None) -> dict[str, Any]:
    """
    What moving up to `plan` costs and covers, before paying (J3-08): the quote, the period it buys
    (today until one cycle later plus the paid days carried over) and the UPI QR / link for the exact amount.
    Raises `UpgradeNotAllowed` / `PaymentUnderReview`.
    """
    now = now or timezone.now()
    subscription = check_upgrade(current_subscription(user), plan)
    due = amount_due(subscription, plan)
    start, until = upgrade_period(subscription, now)
    qr, uri = _upi_for(due, subscription.company)
    return {
        "quote": due.as_dict(),
        "covers_from": timezone.localdate(start).isoformat(),
        "covers_until": timezone.localdate(until).isoformat(),
        "carried_days": carried_days(subscription, now),
        "upi_qr_svg": qr,
        "upi_uri": uri,
    }


def duplicate_of(payment: ManualPayment) -> ManualPayment | None:
    """
    Another payment with the same receipt file (proof hash) or the same staff-typed reference / UTR:
    likely the same money reported twice (J8). None when nothing matches.
    """
    from django.db.models import Q

    match = Q()
    if payment.proof_sha256:
        match |= Q(proof_sha256=payment.proof_sha256)
    reference = (payment.staff_reference or "").strip()
    if reference:
        match |= Q(staff_reference__iexact=reference)
    if not match:
        return None
    return ManualPayment.objects.filter(match).exclude(pk=payment.pk).order_by("-submitted_at", "-id").first()


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
    # The company's country fixes the currency and cycle for the life of the subscription.
    due = pricing.quote(plan, company)
    money = {"currency": due.currency, "billing_cycle": due.billing_cycle}
    if plan.requires_payment and not is_trial:
        subscription = Subscription.objects.create(
            owner=user, company=company, plan=plan, status=Status.PENDING_PAYMENT,
            amount_inr=due.amount, created_at=now, **money,
        )
    else:
        activation = activate_plan(
            user, plan, source="trial" if is_trial else "subscription", trial=is_trial
        )
        subscription = Subscription.objects.create(
            owner=user, company=company, plan=plan, status=Status.ACTIVE,
            amount_inr=0 if is_trial else due.amount, is_trial=is_trial, **money,
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
    accepted_terms = bool(data.pop("accept_terms", False))
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
    due = amount_due(subscription, upgrade_plan)
    _fill_receipt_defaults(data, user, subscription, due)
    payment = ManualPayment(subscription=subscription, is_renewal=renewal, upgrade_plan=upgrade_plan, **data)
    payment.currency = subscription.currency
    payment.due = due.as_dict()
    if accepted_terms:
        payment.terms_accepted_at = timezone.now()
        payment.terms_version = settings.ZINWE_LEGAL_VERSION
    if proof is not None:
        payment.proof = proof
    payment.save()
    transaction.on_commit(lambda: _after_payment_submitted(payment))
    return payment


def _fill_receipt_defaults(data: dict[str, Any], user: User, subscription: Subscription, due: pricing.Quote) -> None:
    """
    The payment page only asks for a receipt (2026-10-16); staff read the details off it. Whatever the
    owner didn't send is filled in: the way they had open (else UPI / Skydo by currency), the amount due,
    today, and their name. The full form (dashboard, older clients) still sends every field.
    """
    if not data.get("method"):
        data["method"] = ManualPayment.Method.UPI if subscription.currency == pricing.INR else ManualPayment.Method.SKYDO
    if data.get("amount_inr") is None:
        data["amount_inr"] = due.total
    if data.get("paid_on") is None:
        data["paid_on"] = timezone.localdate()
    if not (data.get("payer_name") or "").strip():
        company = subscription.company
        data["payer_name"] = (user.full_name or (company.name if company else "") or user.email)[:120]
    data["reference"] = (data.get("reference") or "").strip()


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
    # The owner just sent money: tell them we have the receipt (J3-04).
    notify(
        "email",
        owner.email,
        "payment_received_owner",
        {
            **_owner_links(),
            "first_name": owner.get_short_name() if owner.full_name else "there",
            "payment": payment,
            "plan": payment.upgrade_plan or subscription.plan,
            "amount_text": pricing.money_text(payment.amount_inr, payment.currency),
            "proof_name": os.path.basename(payment.proof.name) if payment.proof else "",
            "is_upgrade": bool(payment.upgrade_plan_id),
            "is_renewal": payment.is_renewal,
            # Only promise a GST invoice when GST is on the quote (not for exports / no seller GSTIN).
            "gst_invoice": bool((payment.due or {}).get("taxes")),
        },
        ref=payment,
    )


# --- Staff review (Django admin) --------------------------------------------


@transaction.atomic
def verify_payment(
    payment_id: int,
    reviewer: User | None,
    staff_reference: str | None = None,
    staff_amount: Any = None,
) -> ManualPayment:
    """
    Accept a submitted payment, owner emailed (with the invoice PDF). Raises `ReviewNotAllowed`.

    `staff_reference` / `staff_amount`: what staff read off the receipt (the admin's approve form requires
    them); stored with `staff_checked_by = reviewer`, and the reference goes on the invoice.

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
    carried = 0
    if payment.upgrade_plan_id and subscription.status in (Status.ACTIVE, Status.EXPIRED):
        new_plan = payment.upgrade_plan
        carried = carried_days(subscription, now) if subscription.status == Status.ACTIVE else 0
        covers_from, ends_at = (
            upgrade_period(subscription, now) if subscription.status == Status.ACTIVE
            else (now, add_months(now, cycle_months(subscription)))
        )
        try:
            activation = activate_plan(
                subscription.owner, new_plan, source="upgrade", allow_paid=True, ends_at=ends_at,
            )
        except PlanNotAllowed as exc:
            raise ReviewNotAllowed(f"Payment #{payment.pk}: {exc}") from exc
        new_amount = amount_due(subscription, new_plan).amount  # before switching: today's price for the new plan
        subscription.plan = new_plan
        subscription.amount_inr = new_amount
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
        covers_from, ends_at = now, add_months(now, cycle_months(subscription))
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
    fields = ["status", "reviewed_at", "reviewed_by", "rejection_reason", "covers_from", "covers_until"]
    if staff_reference is not None or staff_amount is not None:
        payment.staff_reference = (staff_reference or "").strip()[:64]
        payment.staff_amount = staff_amount
        payment.staff_checked_by = reviewer
        fields += ["staff_reference", "staff_amount", "staff_checked_by"]
    payment.save(update_fields=fields)
    from .invoices import issue_invoice  # numbered now: GST invoice numbers run without gaps

    invoice = issue_invoice(payment)
    transaction.on_commit(lambda: _after_payment_verified(payment, subscription, invoice, carried))
    return payment


def _after_payment_verified(
    payment: ManualPayment, subscription: Subscription, invoice: Any = None, carried: int = 0,
) -> None:
    track(
        "payment.verified",
        {"amount_inr": str(payment.amount_inr), "plan": subscription.plan.code, "renewal": payment.is_renewal},
        actor_email=subscription.owner.email,
        ref=payment,
    )
    _send_subscription_active(
        subscription, renewed=payment.is_renewal, payment=payment, invoice=invoice, carried=carried,
    )


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
    subscription = payment.subscription
    owner = subscription.owner
    track("payment.rejected", {}, actor_email=owner.email, ref=payment)
    subscription.refresh_from_db(fields=["status"])
    notify(
        "email",
        owner.email,
        "payment_rejected",
        {
            **_owner_links(),
            "first_name": owner.get_short_name() if owner.full_name else "there",
            "payment": payment,
            "plan": payment.upgrade_plan or subscription.plan,
            "current_plan": subscription.plan,
            "reason": payment.rejection_reason,
            "amount_text": pricing.money_text(payment.amount_inr, payment.currency),
            "reference": (payment.reference or "").strip(),
            # An active plan (renewal / upgrade) keeps running: don't say "still waiting for payment" (J3-05).
            "plan_active": subscription.status == Status.ACTIVE,
            "is_upgrade": bool(payment.upgrade_plan_id),
            "is_renewal": payment.is_renewal,
        },
        ref=payment,
    )


def trial_length(days: int) -> str:
    """90 -> "3-month", 14 -> "14-day" (for "Your free 3-month trial ...")."""
    if days and days % 30 == 0:
        return f"{days // 30}-month"
    return f"{days}-day"


def _owner_links() -> dict[str, str]:
    """Absolute dashboard links for owner emails (each environment links to itself)."""
    app = settings.ZINWE_APP_URL
    return {
        "dashboard_url": f"{app}/app",
        "team_url": f"{app}/app/team",
        "billing_url": f"{app}/app/settings/billing",
    }


def _has_devices(owner: User) -> bool:
    from z_devices.models import Device

    return Device.objects.filter(owner=owner, is_revoked=False).exists()


def _send_subscription_active(
    subscription: Subscription,
    renewed: bool = False,
    payment: ManualPayment | None = None,
    invoice: Any = None,
    carried: int = 0,
) -> None:
    """
    "Your plan is active / renewed" (J3-03, J3-20, J2-05, J5-19): amount, period and invoice number, the GST
    invoice PDF attached, a Billing button, and the "add your first person" step only for owners with no PC.
    """
    owner = subscription.owner
    attachments = []
    amount_text = ""
    if invoice is not None:
        from .invoices import invoice_filename, invoice_pdf

        amount_text = pricing.money_text(invoice.total, invoice.currency)
        try:
            attachments.append((invoice_filename(invoice), invoice_pdf(invoice), "application/pdf"))
        except Exception:  # noqa: BLE001 -- the email still goes; the invoice is on Billing
            logger.exception("Could not render invoice %s for the activation email", invoice.number)
    elif payment is not None:
        amount_text = pricing.money_text(payment.amount_inr, payment.currency)
    notify(
        "email",
        owner.email,
        "subscription_active",
        {
            **_owner_links(),
            "first_name": owner.get_short_name() if owner.full_name else "there",
            "plan": subscription.plan,
            "subscription": subscription,
            "is_trial": subscription.is_trial,
            "is_renewal": renewed,
            "is_upgrade": bool(payment and payment.upgrade_plan_id),
            "trial_length": trial_length(subscription.plan.trial_days),
            "payment": payment,
            "invoice": invoice,
            "invoice_attached": bool(attachments),
            "amount_text": amount_text,
            "covers_from": payment.covers_from if payment else None,
            "covers_until": (payment.covers_until if payment else None) or subscription.ends_at,
            "carried_days": carried,
            "period_word": "year" if cycle_months(subscription) == 12 else "month",
            "has_devices": _has_devices(owner),
        },
        ref=subscription,
        attachments=attachments or None,
    )
