"""Billing by country: INR + GST in India, USD yearly abroad (export), frozen quotes, numbered invoices, PDFs."""
from __future__ import annotations

from datetime import datetime
from datetime import timezone as dt_timezone
from decimal import Decimal

from django.test import override_settings
from rest_framework import status

from . import pricing
from .billing import add_months, verify_payment
from .invoices import amount_in_words, financial_year
from .models import Invoice, ManualPayment, Plan, Subscription
from .test_upgrade import PAY_URL, SUB_URL, _UpgradeBase, payment_form

SELLER = {"name": "SKYTECHNO", "address": "New Delhi, India", "email": "hello@zinwe.io", "gstin": "07ABCDE1234F1Z5"}
GST = {"rate": 18, "seller_state": "Delhi", "sac": "998314", "lut_arn": "AD0710260012345"}


@override_settings(ZINWE_INVOICE_SELLER=SELLER, ZINWE_GST=GST)
class QuoteTests(_UpgradeBase):
    def plan(self, code: str = "startups") -> Plan:
        return Plan.objects.get(code=code)

    def test_india_same_state_is_cgst_plus_sgst(self) -> None:
        self.owner.owned_company.state = "Delhi"
        q = pricing.quote(self.plan(), self.owner.owned_company)
        self.assertEqual((q.currency, q.billing_cycle, q.amount), ("INR", "monthly", Decimal("971.00")))
        self.assertEqual([(t.label, t.amount) for t in q.taxes], [("CGST", Decimal("87.39")), ("SGST", Decimal("87.39"))])
        self.assertEqual(q.total, Decimal("1145.78"))
        self.assertEqual((q.list_amount, q.discount_amount), (Decimal("1349.00"), Decimal("378.00")))

    def test_india_other_state_is_igst(self) -> None:
        self.owner.owned_company.state = "Maharashtra"
        q = pricing.quote(self.plan(), self.owner.owned_company)
        self.assertEqual([(t.label, t.rate, t.amount) for t in q.taxes], [("IGST", Decimal("18"), Decimal("174.78"))])
        self.assertEqual(q.total, Decimal("1145.78"))

    def test_abroad_is_usd_yearly_export(self) -> None:
        company = self.owner.owned_company
        company.country = "United States"
        q = pricing.quote(self.plan("businesses"), company)
        self.assertEqual((q.currency, q.billing_cycle, q.months), ("USD", "yearly", 12))
        self.assertEqual((q.amount, q.list_amount, q.total), (Decimal("408.00"), Decimal("576.00"), Decimal("408.00")))
        self.assertEqual(q.taxes, [])
        self.assertTrue(q.export)
        self.assertIn("LUT ARN AD0710260012345", q.note)

    @override_settings(ZINWE_INVOICE_SELLER={**SELLER, "gstin": ""})
    def test_no_gst_without_a_seller_gstin(self) -> None:
        q = pricing.quote(self.plan(), self.owner.owned_company)
        self.assertEqual((q.taxes, q.total), ([], Decimal("971.00")))
        self.assertEqual(q.note, "GST not charged.")

    def test_free_plan_costs_nothing(self) -> None:
        q = pricing.quote(self.plan("free"), self.owner.owned_company)
        self.assertEqual((q.total, q.taxes), (Decimal("0.00"), []))


@override_settings(ZINWE_INVOICE_SELLER=SELLER, ZINWE_GST=GST)
class FlowTests(_UpgradeBase):
    def test_india_subscription_due_amount_and_invoice(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})
        sub = self.get(SUB_URL).json()["subscription"]
        self.assertEqual((sub["currency"], sub["billing_cycle"]), ("INR", "monthly"))
        self.assertEqual(sub["amount_due"]["total"], "1145.78")

        self.post(PAY_URL, payment_form(amount_inr="1145.78"), fmt="multipart")
        payment = ManualPayment.objects.get()
        self.assertEqual((payment.currency, payment.due["total"]), ("INR", "1145.78"))
        verify_payment(payment.pk, self.staff)

        invoice = Invoice.objects.get()
        self.assertEqual(invoice.data["title"], "Tax invoice")
        self.assertEqual(invoice.data["taxable_amount"], "971.00")
        self.assertEqual([t["label"] for t in invoice.data["taxes"]], ["CGST", "SGST"])  # Delhi customer, Delhi seller
        self.assertEqual(invoice.total, Decimal("1145.78"))
        self.assertTrue(invoice.data["total_in_words"].startswith("Rupees one thousand one hundred forty-five"))

        pdf = self.get(f"/api/auth/subscription/payments/{payment.pk}/invoice/pdf/")
        self.assertEqual(pdf.status_code, status.HTTP_200_OK)
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))

    def test_abroad_pays_usd_for_a_year(self) -> None:
        company = self.owner.owned_company
        company.country = "United States"
        company.state = "California"
        company.save()
        self.post(SUB_URL, {"plan": "startups"})
        sub = Subscription.objects.get()
        self.assertEqual((sub.currency, sub.billing_cycle, sub.amount_inr), ("USD", "yearly", Decimal("144.00")))
        instructions = self.get(SUB_URL).json()["instructions"]
        self.assertEqual(instructions["region"], "INTL")

        self.post(PAY_URL, payment_form(method="skydo", amount_inr="144"), fmt="multipart")
        payment = ManualPayment.objects.get()
        verify_payment(payment.pk, self.staff)
        payment.refresh_from_db()
        self.assertEqual(payment.covers_until, add_months(payment.covers_from, 12))
        invoice = Invoice.objects.get()
        self.assertEqual((invoice.currency, invoice.data["total"], invoice.data["export"]), ("USD", "144.00", True))
        self.assertEqual(invoice.data["title"], "Tax invoice (export of services)")
        self.assertIn("without payment of integrated tax", " ".join(invoice.data["notes"]))

    def test_numbers_run_without_gaps(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})
        self.post(PAY_URL, payment_form(amount_inr="1145.78", reference="A"), fmt="multipart")
        rejected = ManualPayment.objects.get()
        from .billing import reject_payment

        reject_payment(rejected.pk, self.staff, "Not in the bank statement.")  # no number for a rejected payment
        self.post(PAY_URL, payment_form(amount_inr="1145.78", reference="B"), fmt="multipart")
        verify_payment(ManualPayment.objects.get(reference="B").pk, self.staff)
        self.post(PAY_URL, payment_form(amount_inr="1145.78", reference="C"), fmt="multipart")  # renewal
        verify_payment(ManualPayment.objects.get(reference="C").pk, self.staff)
        self.assertEqual(sorted(Invoice.objects.values_list("sequence", flat=True)), [1, 2])

    @override_settings(
        ZINWE_PAYMENT_INSTRUCTIONS={
            "upi_id": "zinwe@okbank", "payee_name": "SKYTECHNO", "bank_name": "", "account_type": "",
            "account_number": "", "ifsc": "", "paypal_url": "", "note": "",
        }
    )
    def test_upi_qr_has_the_amount_due(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})
        qr = self.get("/api/auth/subscription/upi-qr/").json()
        self.assertTrue(qr["svg"].startswith("<svg"))
        self.assertIn(f"am={qr['amount']}", qr["upi_uri"])


class HelperTests(_UpgradeBase):
    def test_financial_year(self) -> None:
        self.assertEqual(financial_year(datetime(2027, 3, 31, 12, tzinfo=dt_timezone.utc)), "2026-27")
        self.assertEqual(financial_year(datetime(2027, 4, 1, 12, tzinfo=dt_timezone.utc)), "2027-28")

    def test_amount_in_words(self) -> None:
        self.assertEqual(
            amount_in_words(Decimal("123456.50"), "INR"),
            "Rupees one lakh twenty-three thousand four hundred fifty-six and fifty paise only",
        )
        self.assertEqual(amount_in_words(Decimal("408.00"), "USD"), "US dollars four hundred eight only")


@override_settings(ZINWE_INVOICE_SELLER=SELLER, ZINWE_GST=GST)
class LockedPriceTests(_UpgradeBase):
    def test_renewal_keeps_the_signed_up_price_and_upgrades_use_todays(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})
        sub = Subscription.objects.get()
        Subscription.objects.filter(pk=sub.pk).update(amount_inr=Decimal("999.00"))  # signed up before a price change
        sub.refresh_from_db()
        renewal = pricing.quote_for_subscription(sub)
        self.assertEqual((renewal.amount, renewal.total), (Decimal("999.00"), Decimal("1178.82")))
        upgrade = pricing.quote_for_subscription(sub, Plan.objects.get(code="businesses"))
        self.assertEqual(upgrade.amount, Decimal("2834.00"))
