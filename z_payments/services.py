"""
Online payment with Razorpay Standard Checkout, alongside -- never instead of -- the manual ways to pay.

The flow:
1. `start_order(user, upgrade_plan)` works out what is due exactly like a receipt upload (`billing.payment_target`),
   creates a Razorpay order for that amount and stores it.
2. The browser opens Razorpay's checkout for that order. On success it sends the three razorpay_* values back:
   `confirm_from_checkout` checks the signature, then `confirm` asks Razorpay itself that the payment is real,
   for this order and this amount (and captures it if it's only authorised).
3. `confirm` turns the paid order into an approved `ManualPayment` (method "razorpay") through
   `billing.verify_payment`: plan, period, invoice and emails are exactly those of a staff-approved receipt.

Safety nets: the webhook (`handle_webhook`) and the hourly `reconcile` job confirm payments whose browser closed
before step 2; and money that can't be applied automatically is left as a submitted payment for staff to approve
(`NEEDS_REVIEW`), with the usual team email. If Razorpay is off or down, the checkout falls back to UPI / bank
transfer (rupees) or Skydo (US dollars) + receipt, which never depend on it. Rupees and US dollars are taken online.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from z_analytics.services import track
from z_auth import billing, pricing
from z_auth.models import Announcement, ManualPayment, Plan, Subscription, User

from . import razorpay_api as api
from .models import RazorpayOrder

log = logging.getLogger(__name__)

LIVE_HOSTS = {"zinwe.io", "www.zinwe.io"}
# The smallest order, in the currency's smallest unit: ₹1.00 or $1.00.
MIN_PAISE = 100
# Razorpay takes rupees, and US dollars from cards abroad (international payments are on, 2026-10-23).
ONLINE_CURRENCIES = (pricing.INR, pricing.USD)
# An order still waiting for payment this long after it was made was abandoned (checkout closed): the hourly
# check marks it failed once Razorpay confirms nothing was paid on it (FIX-004a).
ORDER_EXPIRY = timedelta(hours=24)
ABANDONED_ERROR = "No payment within 24 hours: checkout abandoned."
DONE = (RazorpayOrder.Status.PAID, RazorpayOrder.Status.NEEDS_REVIEW)


class OnlinePaymentUnavailable(Exception):
    """Online payment is switched off, not set up, not for this currency, or Razorpay can't be reached."""

    def __str__(self) -> str:
        return (str(self.args[0]) if self.args else "") or (
            "Online payment isn't available right now. Pay by UPI or bank transfer instead, then upload the receipt."
        )


class OfferNotAvailable(Exception):
    """The admin offer can't be used by this owner now (ended, not theirs, wrong plan, already used)."""

    def __str__(self) -> str:
        return (str(self.args[0]) if self.args else "") or "This offer isn't available any more."


def usable_offer(user: User, offer_id: int, plan: Plan | None) -> Announcement:
    """
    An admin offer (an Announcement with an offer plan and price) this owner may use now for `plan`: live,
    on the dashboard, meant for them (the message's audience), for exactly this plan, not used before.
    Raises `OfferNotAvailable`.
    """
    from z_auth.announcements import _reaches
    from z_auth.entitlements import entitlements_for

    now = timezone.now()
    offer = (
        Announcement.objects.select_related("offer_plan")
        .filter(pk=offer_id, is_active=True, starts_at__lte=now, offer_plan__isnull=False, offer_price_inr__isnull=False)
        .exclude(placement=Announcement.Placement.WEBSITE)
        .first()
    )
    if offer is None or (offer.ends_at is not None and offer.ends_at <= now):
        raise OfferNotAvailable()
    if plan is None or offer.offer_plan_id != plan.pk:
        raise OfferNotAvailable(f"This offer is for the {offer.offer_plan.name} plan.")
    if offer.offer_price_inr < Decimal("1.00"):
        raise OfferNotAvailable()
    if not _reaches(offer, entitlements_for(user), user.email):
        raise OfferNotAvailable()
    used = RazorpayOrder.objects.filter(
        owner=user, offer=offer, status__in=(RazorpayOrder.Status.PAID, RazorpayOrder.Status.NEEDS_REVIEW)
    )
    if used.exists():
        raise OfferNotAvailable("You've already used this offer.")
    return offer


def offer_quote(subscription: Subscription, offer: Announcement) -> pricing.Quote:
    """What the owner pays with the offer: the offer plan at the offer price, for one cycle of their own
    subscription (its currency and billing cycle), with tax. The price is in rupees: customers billed in US dollars
    can't use it."""
    if subscription.currency != pricing.INR:
        raise OfferNotAvailable("This offer is for payments in rupees.")
    return pricing.quote(
        offer.offer_plan, subscription.company, subscription.currency, subscription.billing_cycle,
        cycle_amount=offer.offer_price_inr,
    )


class PaymentNotConfirmed(Exception):
    """Razorpay says this payment isn't (yet) a captured payment of this order for the full amount."""


def online_enabled() -> bool:
    """Keys set, not switched off, and no test keys on the live site."""
    if not (settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET and settings.ZINWE_RAZORPAY_ENABLED):
        return False
    live = (urlparse(settings.ZINWE_APP_URL).hostname or "") in LIVE_HOSTS
    if live and settings.RAZORPAY_KEY_ID.startswith("rzp_test_") and not settings.ZINWE_RAZORPAY_ALLOW_TEST:
        return False
    return True


def checkout_config(currency: str) -> dict[str, str] | None:
    """For the payment page: the public key id, or None when online payment isn't offered (off, or a currency
    Razorpay doesn't take for us)."""
    if currency not in ONLINE_CURRENCIES or not online_enabled():
        return None
    return {"provider": "razorpay", "key_id": settings.RAZORPAY_KEY_ID}


# --- 1. The order --------------------------------------------------------------------------------------


@transaction.atomic
def start_order(
    user: User, upgrade_plan: Plan | None = None, accept_terms: bool = False, offer_id: int | None = None,
    cycle: str | None = None,
) -> RazorpayOrder:
    """A Razorpay order for what `user` owes now (or for an admin offer's price). Raises the billing errors,
    `OfferNotAvailable` or `OnlinePaymentUnavailable`."""
    if not online_enabled():
        raise OnlinePaymentUnavailable()
    offer = usable_offer(user, offer_id, upgrade_plan) if offer_id else None
    # An offer is priced for one cycle of the subscription's own; otherwise the cycle chosen at checkout.
    subscription, upgrade_plan, renewal, due = billing.payment_target(user, upgrade_plan, None if offer else cycle)
    if offer is not None:
        # Same subscription, plan change and period rules as a normal payment; only the price is the offer's.
        # After it's paid, renewals use the plan's normal price (billing.verify_payment re-prices the plan).
        due = offer_quote(subscription, offer)
    if due.currency not in ONLINE_CURRENCIES:
        raise OnlinePaymentUnavailable()
    amount_paise = int((due.total * 100).quantize(Decimal("1")))
    if amount_paise < MIN_PAISE:
        raise billing.NothingToPay()
    receipt = f"zw{subscription.pk}-{int(time.time())}"[:40]
    kind = "upgrade" if upgrade_plan else "renewal" if renewal else "subscription"
    try:
        order = api.create_order(amount_paise, due.currency, receipt, {
            "subscription": str(subscription.pk), "plan": due.plan_code, "kind": kind, "owner": user.email,
        })
    except api.RazorpayError as exc:
        log.warning("Razorpay order failed for subscription %s: %s", subscription.pk, exc)
        raise OnlinePaymentUnavailable() from exc
    return RazorpayOrder.objects.create(
        owner=user, subscription=subscription, upgrade_plan=upgrade_plan, is_renewal=renewal,
        due=due.as_dict(), amount_paise=amount_paise, currency=due.currency, order_id=order["id"],
        receipt=receipt, terms_accepted=accept_terms, offer=offer,
    )


def checkout_options(order: RazorpayOrder) -> dict[str, Any]:
    """What the browser passes to Razorpay's checkout (the key id is public)."""
    owner = order.owner
    company = order.subscription.company
    plan = order.due.get("plan_name") or order.subscription.plan.name
    what = "upgrade" if order.upgrade_plan_id else "renewal" if order.is_renewal else "plan"
    return {
        "key_id": settings.RAZORPAY_KEY_ID,
        "order_id": order.order_id,
        "amount": order.amount_paise,
        "currency": order.currency,
        "name": "Zinwe",
        "description": f"Zinwe {plan} {what}",
        "prefill": {
            "name": owner.full_name or (company.name if company else ""),
            "email": owner.email,
            "contact": (getattr(company, "phone", "") or "").replace(" ", ""),
        },
    }


# --- 2-3. Confirming ---------------------------------------------------------------------------------


@dataclass
class ConfirmResult:
    order: RazorpayOrder
    payment: ManualPayment | None = None


def confirm_from_checkout(user: User, order_id: str, payment_id: str, signature: str) -> ConfirmResult:
    """
    The checkout's success callback. Raises `RazorpayOrder.DoesNotExist` (not this owner's order),
    `PaymentNotConfirmed` (bad signature, or Razorpay doesn't confirm it) or `api.RazorpayError` (couldn't ask:
    the money may well be taken, so the caller says "confirming" and the webhook / hourly check finish it).
    """
    order = RazorpayOrder.objects.get(order_id=order_id, owner=user)
    if not api.checkout_signature_ok(order_id, payment_id, signature):
        raise PaymentNotConfirmed("The payment confirmation didn't match. Nothing was changed.")
    return confirm(order.pk, payment_id, via="checkout")


def confirm(order_pk: int, payment_id: str, via: str) -> ConfirmResult:
    """
    Make a Razorpay payment count, once: check it with Razorpay, capture it if needed, then record it as an
    approved payment. Idempotent (a second call for a paid order returns it unchanged).

    The Razorpay calls happen outside any transaction (FIX-004b: no row lock is held during a 20 s network call);
    the order is then locked and re-checked, so when the checkout, the webhook and the hourly check race, only the
    first one records the payment and the others return it.
    """
    order = RazorpayOrder.objects.get(pk=order_pk)
    if order.status in DONE:
        return ConfirmResult(order, order.payment)
    _confirm_with_razorpay(order, payment_id)  # network; the order's id / amount / currency never change
    with transaction.atomic():
        order = RazorpayOrder.objects.select_for_update().select_related("subscription__owner").get(pk=order_pk)
        if order.status in DONE:
            return ConfirmResult(order, order.payment)
        return _record(order, payment_id, via)


def _confirm_with_razorpay(order: RazorpayOrder, payment_id: str) -> None:
    """Razorpay agrees the payment is this order's, for the full amount, and captured (captures it if only
    authorised). Raises `PaymentNotConfirmed` or `api.RazorpayError`."""
    payment_info = api.fetch_payment(payment_id)
    _check_payment(order, payment_info)
    if payment_info.get("status") != "authorized":
        return
    try:
        payment_info = api.capture(payment_id, order.amount_paise, order.currency)
    except api.RazorpayError:
        # Another caller may have captured it a moment ago: what does Razorpay say now?
        payment_info = api.fetch_payment(payment_id)
        if payment_info.get("status") != "captured":
            raise
    _check_payment(order, payment_info)


def _check_payment(order: RazorpayOrder, info: dict[str, Any]) -> None:
    if info.get("order_id") != order.order_id:
        raise PaymentNotConfirmed("That payment belongs to a different order.")
    if int(info.get("amount") or 0) != order.amount_paise or info.get("currency") != order.currency:
        raise PaymentNotConfirmed("The amount paid doesn't match what was due.")
    if info.get("status") not in ("captured", "authorized"):
        raise PaymentNotConfirmed(f"Razorpay says the payment is {info.get('status') or 'unknown'}.")


def _record(order: RazorpayOrder, payment_id: str, via: str) -> ConfirmResult:
    """Inside confirm's transaction: the approved ManualPayment (or one left for staff)."""
    subscription = order.subscription
    owner = subscription.owner
    company = subscription.company
    now = timezone.now()
    amount = Decimal(order.amount_paise) / 100
    payment = ManualPayment.objects.create(
        subscription=subscription,
        method=ManualPayment.Method.RAZORPAY,
        amount_inr=amount,
        currency=order.currency,
        due=order.due,
        reference=payment_id,
        paid_on=timezone.localdate(),
        payer_name=(owner.full_name or (company.name if company else "") or owner.email)[:120],
        is_renewal=order.is_renewal,
        upgrade_plan=order.upgrade_plan,
        terms_accepted_at=now if order.terms_accepted else None,
        terms_version=settings.ZINWE_LEGAL_VERSION if order.terms_accepted else "",
    )
    order.razorpay_payment_id = payment_id
    order.payment = payment
    order.error = ""  # a late payment on an order marked abandoned still counts
    order.paid_at = now
    order.confirmed_via = via
    try:
        payment = billing.verify_payment(payment.pk, None, staff_reference=payment_id, staff_amount=amount)
        order.status = RazorpayOrder.Status.PAID
        event = "payment.online"
    except billing.ReviewNotAllowed as exc:
        # Paid, but the plan changed since the order was made: staff decide, like a receipt. Never lost.
        order.status = RazorpayOrder.Status.NEEDS_REVIEW
        order.error = str(exc)[:500]
        event = "payment.online_review"
        transaction.on_commit(lambda: billing._after_payment_submitted(payment))
    order.save(update_fields=["razorpay_payment_id", "payment", "paid_at", "confirmed_via", "status", "error"])
    transaction.on_commit(lambda: track(
        event, {"amount_inr": str(amount), "plan": order.due.get("plan_code", ""), "via": via},
        actor_email=owner.email, ref=payment,
    ))
    return ConfirmResult(order, payment)


# --- Safety nets -----------------------------------------------------------------------------------------


def handle_webhook(body: bytes, signature: str) -> str:
    """Razorpay webhook (payment.captured / order.paid). Returns what happened, for the log."""
    import json

    if not api.webhook_signature_ok(body, signature):
        raise PaymentNotConfirmed("Bad webhook signature.")
    event = json.loads(body or b"{}")
    entity = (event.get("payload", {}).get("payment") or {}).get("entity") or {}
    order_id, payment_id = entity.get("order_id"), entity.get("id")
    if event.get("event") not in ("payment.captured", "order.paid", "payment.authorized") or not order_id:
        return "ignored"
    order = RazorpayOrder.objects.filter(order_id=order_id).first()
    if order is None:
        return "unknown order"
    result = confirm(order.pk, payment_id, via="webhook")
    return result.order.status


@dataclass
class ReconcileResult:
    checked: int = 0
    confirmed: int = 0
    expired: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        text = f"{self.checked} open order(s) checked, {self.confirmed} confirmed, {self.expired} expired"
        return text + (f", {len(self.errors)} error(s)" if self.errors else "")


def reconcile(now: Any = None) -> ReconcileResult:
    """
    The hourly check (run_jobs): open orders whose payment Razorpay has taken but we never heard about (browser
    closed, webhook missed) are confirmed now. Orders older than `ORDER_EXPIRY` with nothing paid on them are marked
    failed (abandoned checkouts; FIX-004a) -- only when Razorpay answered, never because it couldn't be reached.
    A payment that still arrives later (webhook / checkout) is confirmed as usual.
    """
    result = ReconcileResult()
    if not online_enabled():
        return result
    now = now or timezone.now()
    open_orders = RazorpayOrder.objects.filter(
        status=RazorpayOrder.Status.CREATED,
        created_at__lte=now - timedelta(minutes=5),
    )
    for order in open_orders:
        result.checked += 1
        try:
            paid = [p for p in api.order_payments(order.order_id) if p.get("status") in ("captured", "authorized")]
            if paid:
                confirm(order.pk, paid[0]["id"], via="reconcile")
                result.confirmed += 1
            elif order.created_at <= now - ORDER_EXPIRY:
                result.expired += RazorpayOrder.objects.filter(
                    pk=order.pk, status=RazorpayOrder.Status.CREATED,
                ).update(status=RazorpayOrder.Status.FAILED, error=ABANDONED_ERROR)
        except (api.RazorpayError, PaymentNotConfirmed) as exc:
            result.errors.append(f"{order.order_id}: {exc}")
    return result
