"""Admin → Subscriptions: billing view, the per-customer payment link, invoice PDFs."""
from __future__ import annotations

from django.test import override_settings
from django.utils import timezone
from rest_framework import status

from z_auth.billing import subscribe, submit_payment, verify_payment
from z_auth.models import Company, ManualPayment, Plan, Subscription

from .tests import AdminApiTestCase

LIST_URL = "/api/admin/subscriptions/"
SELLER = {"name": "SKYTECHNO", "address": "New Delhi, India", "email": "hello@zinwe.io", "gstin": "07ABCDE1234F1Z5"}


@override_settings(ZINWE_INVOICE_SELLER=SELLER)
class SubscriptionAdminTests(AdminApiTestCase):
    def customer(self, email: str, country: str = "India", state: str = "Delhi") -> Subscription:
        owner = self.make_owner(email)
        Company.objects.create(
            owner=owner, name=f"{email} Ltd", industry="it_services", team_size="1-10", phone="+1 555 0100",
            city="Austin" if country != "India" else "New Delhi", state=state, country=country,
        )
        return subscribe(owner, Plan.objects.get(code="startups"))

    def test_list_shows_region_currency_and_amount_due(self) -> None:
        self.customer("asha@acme.in")
        self.customer("joe@acme.com", country="United States", state="Texas")
        rows = {r["owner"]["email"]: r for r in self.client.get(LIST_URL).json()}
        india, us = rows["asha@acme.in"], rows["joe@acme.com"]
        self.assertEqual((india["region"], india["currency"], india["amount_due"]["total"]), ("IN", "INR", "1145.78"))
        self.assertEqual((us["region"], us["currency"], us["billing_cycle"]), ("INTL", "USD", "yearly"))
        self.assertEqual(us["amount_due"]["total"], "144.00")

    def test_payment_link_shows_on_the_customer_page(self) -> None:
        sub = self.customer("joe@acme.com", country="United States", state="Texas")
        bad = self.client.patch(f"{LIST_URL}{sub.pk}/", {"payment_link": "skydo.com/pay/abc"}, format="json")
        self.assertEqual(bad.status_code, status.HTTP_400_BAD_REQUEST)
        ok = self.client.patch(f"{LIST_URL}{sub.pk}/", {"payment_link": "https://pay.skydo.com/abc"}, format="json")
        self.assertEqual(ok.json()["payment_link"], "https://pay.skydo.com/abc")
        self.as_user(sub.owner)
        instructions = self.client.get("/api/auth/subscription/").json()["instructions"]
        self.assertEqual(instructions["payment_link"], "https://pay.skydo.com/abc")

    def test_invoice_pdf_and_list(self) -> None:
        sub = self.customer("asha@acme.in")
        payment = submit_payment(
            sub.owner,
            {"method": "upi", "amount_inr": "1145.78", "reference": "UTR1", "paid_on": timezone.localdate(),
             "payer_name": "Asha", "accept_terms": True},
        )
        with self.captureOnCommitCallbacks(execute=True):
            verify_payment(payment.pk, self.staff)
        row = self.client.get(LIST_URL).json()[0]
        self.assertEqual(len(row["invoices"]), 1)
        pdf = self.client.get(f"/api/admin/payments/{payment.pk}/invoice/pdf/")
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))

    def test_pdf_only_once_confirmed(self) -> None:
        sub = self.customer("asha@acme.in")
        payment = ManualPayment.objects.create(
            subscription=sub, method="upi", amount_inr=1, reference="x", paid_on=timezone.localdate(), payer_name="A",
        )
        self.assertEqual(
            self.client.get(f"/api/admin/payments/{payment.pk}/invoice/pdf/").status_code, status.HTTP_409_CONFLICT
        )

    def test_staff_only(self) -> None:
        self.as_user(self.make_owner())
        self.assertEqual(self.client.get(LIST_URL).status_code, status.HTTP_403_FORBIDDEN)
