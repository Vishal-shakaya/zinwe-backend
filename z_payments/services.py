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
transfer + receipt, which never depend on it.
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
from z_auth.models import ManualPayment, Plan, User

from . import razorpay_api as api
from .models import RazorpayOrder

log = logging.getLogger(__name__)

LIVE_HOSTS = {"zinwe.io", "www.zinwe.io"}
MIN_PAISE = 100


class OnlinePaymentUnavailable(Exception):
    """Online payment is switched off, not set up, not for this currency, or Razorpay can't be reached."""

    def __str__(self) -> str:
        return (str(self.args[0]) if self.args else "") or (
            "Online payment isn't available right now. Pay by UPI or bank transfer instead, then upload the receipt."
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
    """For the payment page: the public key id, or None when online payment isn't offered (USD, or off)."""
    if currency != pricing.INR or not online_enabled():
        return None
    return {"provider": "razorpay", "key_id": settings.RAZORPAY_KEY_ID}


# --- 1. The order --------------------------------------------------------------------------------------


@transaction.atomic
def start_order(user: User, upgrade_plan: Plan | None = None, accept_terms: bool = False) -> RazorpayOrder:
    """A Razorpay order for what `user` owes now. Raises the billing errors, or `OnlinePaymentUnavailable`."""
    if not online_enabled():
        raise OnlinePaymentUnavailable()
    subscription, upgrade_plan, renewal, due = billing.payment_target(user, upgrade_plan)
    if due.currency != pricing.INR:
        raise OnlinePaymentUnavailable("Online payment is for payments in rupees. Use Skydo for US dollars.")
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
        receipt=receipt, terms_accepted=accept_terms,
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
    """
    with transaction.atomic():
        order = RazorpayOrder.objects.select_for_update().select_related("subscription__owner").get(pk=order_pk)
        if order.status in (RazorpayOrder.Status.PAID, RazorpayOrder.Status.NEEDS_REVIEW):
            return ConfirmResult(order, order.payment)
        payment_info = api.fetch_payment(payment_id)
        _check_payment(order, payment_info)
        if payment_info.get("status") == "authorized":
            payment_info = api.capture(payment_id, order.amount_paise, order.currency)
            _check_payment(order, payment_info)
        result = _record(order, payment_id, via)
    return result


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
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        text = f"{self.checked} open order(s) checked, {self.confirmed} confirmed"
        return text + (f", {len(self.errors)} error(s)" if self.errors else "")


def reconcile(now: Any = None) -> ReconcileResult:
    """
    The hourly check (run_jobs): open orders from the last 3 days whose payment Razorpay has taken but we never
    heard about (browser closed, webhook missed) are confirmed now.
    """
    result = ReconcileResult()
    if not online_enabled():
        return result
    now = now or timezone.now()
    open_orders = RazorpayOrder.objects.filter(
        status=RazorpayOrder.Status.CREATED,
        created_at__gte=now - timedelta(days=3),
        created_at__lte=now - timedelta(minutes=5),
    )
    for order in open_orders:
        result.checked += 1
        try:
            paid = [p for p in api.order_payments(order.order_id) if p.get("status") in ("captured", "authorized")]
            if paid:
                confirm(order.pk, paid[0]["id"], via="reconcile")
                result.confirmed += 1
        except (api.RazorpayError, PaymentNotConfirmed) as exc:
            result.errors.append(f"{order.order_id}: {exc}")
    return result
