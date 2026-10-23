"""Monthly or yearly, chosen at checkout (2026-10-23): yearly prices, previews, receipts, Razorpay, cycle changes."""
from __future__ import annotations

from decimal import Decimal

from django.test import override_settings
from rest_framework import status

from . import pricing
from .billing import add_months, verify_payment
from .models import ManualPayment, Plan, Subscription
from .test_upgrade import PAY_URL, SUB_URL, _UpgradeBase, payment_form

CHECKOUT_URL = "/api/auth/subscription/checkout/"
SELLER = {"name": "SKYTECHNO", "address": "New Delhi, India", "email": "hello@zinwe.io", "gstin": "07ABCDE1234F1Z5"}


@override_settings(ZINWE_INVOICE_SELLER=SELLER)
class YearlyPricingTests(_UpgradeBase):
    def test_a_year_is_the_yearly_price_against_12_list_months(self) -> None:
        plan = Plan.objects.get(code="startups")  # ₹971 a month (₹1,349 list); a year seeded at 10 months
        q = pricing.quote(plan, self.owner.owned_company, cycle="yearly")
        self.assertEqual((q.billing_cycle, q.months, q.amount, q.list_amount), ("yearly", 12, Decimal("9710.00"), Decimal("16188.00")))
        self.assertEqual(pricing.yearly_saving_percent(plan, "INR"), 17)

    def test_no_yearly_price_means_12_months(self) -> None:
        plan = Plan.objects.get(code="startups")
        plan.price_inr_yearly = None
        plan.save()
        q = pricing.quote(plan, self.owner.owned_company, cycle="yearly")
        self.assertEqual(q.amount, Decimal("11652.00"))
        self.assertEqual(pricing.yearly_saving_percent(plan, "INR"), 0)

    def test_checkout_preview_for_each_cycle(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})
        monthly = self.get(CHECKOUT_URL).json()
        yearly = self.get(f"{CHECKOUT_URL}?cycle=yearly").json()
        self.assertEqual((monthly["quote"]["billing_cycle"], monthly["quote"]["amount"]), ("monthly", "971.00"))
        self.assertEqual((yearly["quote"]["billing_cycle"], yearly["quote"]["amount"]), ("yearly", "9710.00"))
        self.assertEqual(yearly["yearly_saving_percent"], 17)
        self.assertEqual(yearly["covers_until"], add_months(Subscription.objects.get().created_at, 12).date().isoformat())
        self.assertEqual(Subscription.objects.get().billing_cycle, "monthly")  # looking never changes anything
        # Another plan instead of the one waiting for payment.
        teams = self.get(f"{CHECKOUT_URL}?plan=teams&cycle=yearly").json()
        self.assertEqual((teams["quote"]["plan_code"], teams["quote"]["amount"]), ("teams", "17990.00"))

    def test_nothing_to_pay(self) -> None:
        self.assertEqual(self.get(CHECKOUT_URL).status_code, status.HTTP_409_CONFLICT)

    def test_first_payment_yearly(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})
        response = self.post(PAY_URL, payment_form(amount_inr="11457.80", billing_cycle="yearly"), fmt="multipart")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        payment = ManualPayment.objects.get()
        self.assertEqual(payment.due["billing_cycle"], "yearly")
        verify_payment(payment.pk, self.staff)
        payment.refresh_from_db()
        sub = Subscription.objects.get()
        self.assertEqual((sub.billing_cycle, sub.amount_inr, sub.status), ("yearly", Decimal("9710.00"), "active"))
        self.assertEqual(payment.covers_until, add_months(payment.covers_from, 12))

    def test_renewing_a_monthly_plan_for_a_year(self) -> None:
        sub = self.start_startups()
        ends = sub.ends_at
        self.post(PAY_URL, payment_form(amount_inr="11457.80", reference="UTR-Y", billing_cycle="yearly"), fmt="multipart")
        verify_payment(ManualPayment.objects.get(reference="UTR-Y").pk, self.staff)
        sub.refresh_from_db()
        self.assertEqual((sub.billing_cycle, sub.amount_inr), ("yearly", Decimal("9710.00")))
        self.assertEqual(sub.ends_at, add_months(ends, 12))
        # The next renewal follows the new cycle.
        self.assertEqual(self.get(CHECKOUT_URL).json()["quote"]["billing_cycle"], "yearly")

    def test_upgrade_for_a_year(self) -> None:
        self.start_startups()
        preview = self.get(f"{CHECKOUT_URL}?plan=teams&cycle=yearly").json()
        self.assertEqual((preview["quote"]["amount"], preview["quote"]["billing_cycle"]), ("17990.00", "yearly"))
        self.post(PAY_URL, payment_form(amount_inr="21228.20", reference="UTR-UP", upgrade_plan="teams", billing_cycle="yearly"), fmt="multipart")
        payment = ManualPayment.objects.get(reference="UTR-UP")
        verify_payment(payment.pk, self.staff)
        sub = Subscription.objects.get(status="active")
        self.assertEqual((sub.plan.code, sub.billing_cycle, sub.amount_inr), ("teams", "yearly", Decimal("17990.00")))


@override_settings(
    RAZORPAY_KEY_ID="rzp_test_key", RAZORPAY_KEY_SECRET="s3cret", ZINWE_RAZORPAY_ENABLED=True,
    ZINWE_APP_URL="https://dev.frontend.zinwe.io", ZINWE_INVOICE_SELLER=SELLER,
)
class YearlyOnlineTests(_UpgradeBase):
    def test_razorpay_order_for_a_year(self) -> None:
        from unittest import mock

        from z_payments import razorpay_api as api
        from z_payments.models import RazorpayOrder

        self.post(SUB_URL, {"plan": "startups"})
        fake = {"id": "order_y", "amount": 0, "currency": "INR"}
        with mock.patch.object(api, "create_order", return_value=fake) as create:
            response = self.post("/api/payments/razorpay/order/", {"accept_terms": True, "billing_cycle": "yearly"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        order = RazorpayOrder.objects.get()
        self.assertEqual(order.due["billing_cycle"], "yearly")
        self.assertEqual(order.amount_paise, int(Decimal(order.due["total"]) * 100))
        self.assertEqual(create.call_args.args[0], order.amount_paise)
