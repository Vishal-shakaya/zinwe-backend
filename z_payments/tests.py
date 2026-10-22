"""Online payment with Razorpay: orders, checkout confirmation, webhook, hourly check, and the manual fallback."""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import override_settings
from rest_framework import status

from z_auth._testing import PortalTestCase
from z_auth.models import ManualPayment, Subscription, User

from . import razorpay_api as api
from .models import RazorpayOrder
from .services import reconcile

SUB_URL = "/api/auth/subscription/"
PAY_URL = "/api/auth/subscription/payment/"
ORDER_URL = "/api/payments/razorpay/order/"
VERIFY_URL = "/api/payments/razorpay/verify/"
WEBHOOK_URL = "/api/payments/razorpay/webhook/"
KEYS = dict(
    RAZORPAY_KEY_ID="rzp_test_key", RAZORPAY_KEY_SECRET="s3cret", RAZORPAY_WEBHOOK_SECRET="hook",
    ZINWE_RAZORPAY_ENABLED=True, ZINWE_APP_URL="https://dev.frontend.zinwe.io",
)


def sign(message: str, secret: str = "s3cret") -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


@override_settings(**KEYS)
class RazorpayTests(PortalTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.owner = self.make_owner()
        self.make_company(self.owner)
        self.staff = User.objects.create_superuser("ops@zinwe.local", "ops-12345678")
        self.orders = 0
        patcher = mock.patch.object(api, "create_order", side_effect=self.fake_order)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_order(self, amount, currency, receipt, notes):  # type: ignore[no-untyped-def]
        self.orders += 1
        return {"id": f"order_{self.orders}", "amount": amount, "currency": currency}

    def start(self, **body):  # type: ignore[no-untyped-def]
        self.post(SUB_URL, {"plan": "startups"})
        return self.post(ORDER_URL, {"accept_terms": True, **body})

    def paid(self, order: RazorpayOrder, status_: str = "captured", **over):  # type: ignore[no-untyped-def]
        return {"id": "pay_1", "order_id": order.order_id, "amount": order.amount_paise, "currency": "INR",
                "status": status_, **over}

    def verify(self, order: RazorpayOrder, signature: str | None = None):  # type: ignore[no-untyped-def]
        return self.post(VERIFY_URL, {
            "razorpay_order_id": order.order_id, "razorpay_payment_id": "pay_1",
            "razorpay_signature": signature or sign(f"{order.order_id}|pay_1"),
        })

    # --- the order -------------------------------------------------------------------------------------

    def test_order_for_what_is_due(self) -> None:
        response = self.start()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        body = response.json()
        due = Decimal(self.get(SUB_URL).json()["subscription"]["amount_due"]["total"])
        self.assertEqual((body["key_id"], body["order_id"], body["currency"]), ("rzp_test_key", "order_1", "INR"))
        self.assertEqual(body["amount"], int(due * 100))
        self.assertEqual(body["prefill"]["email"], self.owner.email)
        self.assertNotIn("s3cret", json.dumps(body))
        self.assertEqual(RazorpayOrder.objects.get().amount_paise, int(due * 100))

    def test_terms_are_required(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})
        response = self.post(ORDER_URL, {"accept_terms": False})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(RazorpayOrder.objects.exists())

    def test_instructions_offer_online_payment(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})
        self.assertEqual(self.get(SUB_URL).json()["instructions"]["online"], {"provider": "razorpay", "key_id": "rzp_test_key"})

    def test_off_means_manual_only(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})
        with self.settings(ZINWE_RAZORPAY_ENABLED=False):
            self.assertIsNone(self.get(SUB_URL).json()["instructions"]["online"])
            response = self.post(ORDER_URL, {"accept_terms": True})
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertTrue(response.json()["fallback"])
        # The receipt upload still works.
        self.assertEqual(self.get(SUB_URL).json()["instructions"]["upi_id"] is not None, True)

    def test_test_keys_never_on_the_live_site(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})
        with self.settings(ZINWE_APP_URL="https://zinwe.io"):
            self.assertIsNone(self.get(SUB_URL).json()["instructions"]["online"])
            self.assertEqual(self.post(ORDER_URL, {"accept_terms": True}).status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_razorpay_down_falls_back(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})
        with mock.patch.object(api, "create_order", side_effect=api.RazorpayError("down", 502)):
            response = self.post(ORDER_URL, {"accept_terms": True})
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertTrue(response.json()["fallback"])

    def test_usd_customers_use_skydo(self) -> None:
        company = self.owner.owned_company
        company.country = "United States"
        company.save()
        response = self.start()
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertIsNone(self.get(SUB_URL).json()["instructions"]["online"])

    def test_not_while_a_receipt_is_being_checked(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})
        from z_auth.test_owner_emails import payment_form

        self.post(PAY_URL, payment_form(), fmt="multipart")
        self.assertEqual(self.post(ORDER_URL, {"accept_terms": True}).status_code, status.HTTP_409_CONFLICT)

    # --- confirming --------------------------------------------------------------------------------------

    def test_paid_switches_the_plan_on_with_an_invoice(self) -> None:
        self.start()
        order = RazorpayOrder.objects.get()
        with mock.patch.object(api, "fetch_payment", return_value=self.paid(order)):
            response = self.verify(order)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertEqual(response.json()["status"], "paid")
        payment = ManualPayment.objects.get()
        self.assertEqual((payment.method, payment.status, payment.reference), ("razorpay", "verified", "pay_1"))
        self.assertEqual(payment.amount_inr, Decimal(order.amount_paise) / 100)
        self.assertIsNotNone(payment.terms_accepted_at)
        self.assertEqual(Subscription.objects.get().status, Subscription.Status.ACTIVE)
        self.assertTrue(payment.invoice.number)  # the GST invoice, numbered as for an approved receipt
        order.refresh_from_db()
        self.assertEqual((order.status, order.confirmed_via), ("paid", "checkout"))
        # A second confirmation (double click, webhook) changes nothing.
        with mock.patch.object(api, "fetch_payment", return_value=self.paid(order)) as again:
            self.assertEqual(self.verify(order).json()["status"], "paid")
        again.assert_not_called()
        self.assertEqual(ManualPayment.objects.count(), 1)

    def test_bad_signature_changes_nothing(self) -> None:
        self.start()
        order = RazorpayOrder.objects.get()
        with mock.patch.object(api, "fetch_payment", return_value=self.paid(order)) as fetch:
            response = self.verify(order, signature="0" * 64)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        fetch.assert_not_called()
        self.assertFalse(ManualPayment.objects.exists())
        self.assertEqual(Subscription.objects.get().status, Subscription.Status.PENDING_PAYMENT)

    def test_missing_fields(self) -> None:
        self.assertEqual(self.post(VERIFY_URL, {"razorpay_order_id": "order_1"}).status_code, status.HTTP_400_BAD_REQUEST)

    def test_someone_elses_order(self) -> None:
        self.start()
        order = RazorpayOrder.objects.get()
        self.make_owner("other@acme.in")
        self.assertEqual(self.verify(order).status_code, status.HTTP_404_NOT_FOUND)

    def test_razorpay_must_agree_on_amount_and_order(self) -> None:
        self.start()
        order = RazorpayOrder.objects.get()
        for wrong in (self.paid(order, amount=100), self.paid(order, order_id="order_x"), self.paid(order, status_="failed")):
            with mock.patch.object(api, "fetch_payment", return_value=wrong):
                self.assertEqual(self.verify(order).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(ManualPayment.objects.exists())

    def test_authorised_payments_are_captured(self) -> None:
        self.start()
        order = RazorpayOrder.objects.get()
        with mock.patch.object(api, "fetch_payment", return_value=self.paid(order, status_="authorized")), \
                mock.patch.object(api, "capture", return_value=self.paid(order)) as capture:
            self.assertEqual(self.verify(order).json()["status"], "paid")
        capture.assert_called_once_with("pay_1", order.amount_paise, "INR")

    def test_razorpay_unreachable_after_paying_is_never_a_failure(self) -> None:
        self.start()
        order = RazorpayOrder.objects.get()
        with mock.patch.object(api, "fetch_payment", side_effect=api.RazorpayError("timeout")):
            response = self.verify(order)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.json()["status"], "confirming")
        # The hourly check finishes it.
        RazorpayOrder.objects.filter(pk=order.pk).update(created_at=order.created_at - timedelta(minutes=10))
        with mock.patch.object(api, "order_payments", return_value=[self.paid(order)]), \
                mock.patch.object(api, "fetch_payment", return_value=self.paid(order)):
            result = reconcile()
        self.assertEqual((result.checked, result.confirmed), (1, 1))
        order.refresh_from_db()
        self.assertEqual((order.status, order.confirmed_via), ("paid", "reconcile"))
        self.assertEqual(Subscription.objects.get().status, Subscription.Status.ACTIVE)

    def test_plan_changed_meanwhile_goes_to_staff(self) -> None:
        self.start()
        order = RazorpayOrder.objects.get()
        Subscription.objects.update(status=Subscription.Status.CANCELLED)
        with mock.patch.object(api, "fetch_payment", return_value=self.paid(order)):
            response = self.verify(order)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.json()["status"], "review")
        self.assertEqual(ManualPayment.objects.get().status, "submitted")  # staff approve it like a receipt
        order.refresh_from_db()
        self.assertEqual(order.status, "needs_review")

    def test_upgrade_paid_online(self) -> None:
        self.start()
        order = RazorpayOrder.objects.get()
        with mock.patch.object(api, "fetch_payment", return_value=self.paid(order)):
            self.verify(order)
        response = self.post(ORDER_URL, {"accept_terms": True, "upgrade_plan": "teams"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        upgrade = RazorpayOrder.objects.get(order_id=response.json()["order_id"])
        self.assertEqual(upgrade.upgrade_plan.code, "teams")
        with mock.patch.object(api, "fetch_payment", return_value={**self.paid(upgrade), "id": "pay_2"}):
            self.post(VERIFY_URL, {"razorpay_order_id": upgrade.order_id, "razorpay_payment_id": "pay_2",
                                   "razorpay_signature": sign(f"{upgrade.order_id}|pay_2")})
        self.assertEqual(Subscription.objects.get().plan.code, "teams")

    # --- webhook ---------------------------------------------------------------------------------------

    def test_webhook_confirms_a_closed_browser_payment(self) -> None:
        self.start()
        order = RazorpayOrder.objects.get()
        body = json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": self.paid(order)}}})
        self.use_token(None)
        bad = self.client.post(WEBHOOK_URL, body, content_type="application/json", HTTP_X_RAZORPAY_SIGNATURE="nope")
        self.assertEqual(bad.status_code, status.HTTP_400_BAD_REQUEST)
        with mock.patch.object(api, "fetch_payment", return_value=self.paid(order)):
            ok = self.client.post(WEBHOOK_URL, body, content_type="application/json",
                                  HTTP_X_RAZORPAY_SIGNATURE=sign(body, "hook"))
        self.assertEqual(ok.status_code, status.HTTP_200_OK, ok.content)
        order.refresh_from_db()
        self.assertEqual((order.status, order.confirmed_via), ("paid", "webhook"))
