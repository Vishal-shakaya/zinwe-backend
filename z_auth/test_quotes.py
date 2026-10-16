"""Receipt-only payments, every way to pay on the payment page, and shared quotes (PDF, email, public link)."""
from __future__ import annotations

from decimal import Decimal

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework import status

from .models import ManualPayment, Subscription
from .test_portal import PDF, MediaMixin
from .test_upgrade import PAY_URL, SUB_URL, _UpgradeBase

QUOTE_URL = "/api/auth/subscription/quote/"
EMAIL_URL = "/api/auth/subscription/quote/email/"
SKYDO_PAGE = "https://dashboard.skydo.com/accounts/test?location=usa"
PAYMENT = {
    "upi_id": "zinwe@okbank", "payee_name": "SKYTECHNO", "bank_name": "HDFC Bank", "account_type": "Current",
    "account_number": "50200012345678", "ifsc": "HDFC0001234", "paypal_url": "", "note": "",
}
SKYDO = {
    "account_name": "SKYTECHNO", "bank_name": "Community Federal Savings Bank", "account_number": "8337000000",
    "routing_number": "026073150", "account_type": "Checking", "swift": "CMFGUS33",
}


def receipt() -> SimpleUploadedFile:
    return SimpleUploadedFile("receipt.pdf", PDF, content_type="application/pdf")


@override_settings(ZINWE_PAYMENT_INSTRUCTIONS=PAYMENT, ZINWE_SKYDO_ACCOUNT=SKYDO, ZINWE_SKYDO_PAYMENT_URL=SKYDO_PAGE)
class ReceiptPaymentTests(MediaMixin, _UpgradeBase):
    def test_a_receipt_alone_is_enough(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})
        response = self.post(
            PAY_URL, {"proof": receipt(), "method": "bank_transfer", "accept_terms": "true"}, fmt="multipart"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        payment = ManualPayment.objects.get()
        due = Decimal(payment.due["total"])
        self.assertEqual(payment.method, "bank_transfer")
        self.assertEqual(payment.amount_inr, due)
        self.assertEqual(payment.paid_on, timezone.localdate())
        self.assertEqual(payment.payer_name, "Asha Verma")
        self.assertEqual(payment.reference, "")
        self.assertTrue(payment.proof)
        self.assertIn("read it off the receipt", mail.outbox[0].body)

    def test_method_defaults_by_currency(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})
        self.post(PAY_URL, {"proof": receipt(), "accept_terms": "true"}, fmt="multipart")
        self.assertEqual(ManualPayment.objects.get().method, "upi")

    def test_needs_a_receipt_or_a_reference(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})
        response = self.post(PAY_URL, {"method": "upi", "accept_terms": "true"}, fmt="multipart")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json(), {"proof": ["Upload your payment receipt or screenshot."]})
        self.assertFalse(ManualPayment.objects.exists())

    def test_terms_still_required(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})
        response = self.post(PAY_URL, {"proof": receipt()}, fmt="multipart")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("accept_terms", response.json())

    def test_india_sees_every_way_to_pay(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})
        ins = self.get(SUB_URL).json()["instructions"]
        self.assertEqual(ins["region"], "IN")
        self.assertEqual((ins["upi_id"], ins["ifsc"]), ("zinwe@okbank", "HDFC0001234"))
        self.assertTrue(ins["upi_qr_svg"].startswith("<svg"))
        self.assertEqual(ins["skydo"]["routing_number"], "026073150")
        self.assertEqual(ins["payment_link"], SKYDO_PAGE)

    def test_abroad_also_sees_upi_and_bank(self) -> None:
        company = self.owner.owned_company
        company.country = "United States"
        company.save()
        self.post(SUB_URL, {"plan": "startups"})
        ins = self.get(SUB_URL).json()["instructions"]
        self.assertEqual(ins["region"], "INTL")
        self.assertEqual((ins["upi_id"], ins["account_number"]), ("zinwe@okbank", "50200012345678"))
        self.assertEqual(ins["upi_qr_svg"], "")  # a USD amount can't go in a UPI QR
        self.assertEqual(ins["payment_link"], SKYDO_PAGE)

    def test_customer_payment_link_wins(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})
        Subscription.objects.update(payment_link="https://pay.skydo.com/own-link")
        self.assertEqual(self.get(SUB_URL).json()["instructions"]["payment_link"], "https://pay.skydo.com/own-link")


@override_settings(
    ZINWE_PAYMENT_INSTRUCTIONS=PAYMENT, ZINWE_SKYDO_ACCOUNT=SKYDO, ZINWE_SKYDO_PAYMENT_URL=SKYDO_PAGE,
    ZINWE_APP_URL="https://zinwe.test",
)
class QuoteTests(MediaMixin, _UpgradeBase):
    def test_quote_and_public_link(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})
        doc = self.get(QUOTE_URL).json()
        sub = Subscription.objects.get()
        self.assertTrue(doc["number"].startswith("ZWQ/") and doc["number"].endswith(f"/{sub.pk:05d}"))
        self.assertEqual(doc["public_url"], f"https://zinwe.test/quote/{sub.quote_token}")
        self.assertEqual(doc["quote"]["plan_code"], "startups")
        self.assertEqual(doc["instructions"]["upi_id"], "zinwe@okbank")
        self.assertEqual(doc["customer"]["name"], self.owner.owned_company.name)
        self.assertNotIn("email", doc["customer"])  # the public page doesn't show the owner's email
        # The link is made once and kept.
        self.assertEqual(self.get(QUOTE_URL).json()["public_url"], doc["public_url"])

        self.use_token(None)
        public = self.get(f"/api/auth/quotes/{sub.quote_token}/")
        self.assertEqual(public.status_code, status.HTTP_200_OK)
        self.assertEqual(public.json()["quote"]["total"], doc["quote"]["total"])
        pdf = self.get(f"/api/auth/quotes/{sub.quote_token}/pdf/")
        self.assertEqual(pdf.status_code, status.HTTP_200_OK)
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        self.assertEqual(self.get("/api/auth/quotes/not-a-real-token/").status_code, status.HTTP_404_NOT_FOUND)

    def test_owner_pdf(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})
        pdf = self.get(f"{QUOTE_URL}pdf/")
        self.assertEqual(pdf.status_code, status.HTTP_200_OK)
        self.assertIn("Zinwe-quote-ZWQ-", pdf["Content-Disposition"])

    def test_link_stops_working_when_the_plan_changes(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})
        token = self.get(QUOTE_URL).json()["public_url"].rsplit("/", 1)[1]
        self.post(SUB_URL, {"plan": "free"})  # the pending Startups choice is cancelled
        self.use_token(None)
        self.assertEqual(self.get(f"/api/auth/quotes/{token}/").status_code, status.HTTP_404_NOT_FOUND)

    def test_nothing_due_means_no_quote(self) -> None:
        self.post(SUB_URL, {"plan": "free"})
        self.assertEqual(self.get(QUOTE_URL).status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(self.post(EMAIL_URL, {}).status_code, status.HTTP_409_CONFLICT)

    def test_email_attaches_the_pdf(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})
        mail.outbox.clear()
        response = self.post(EMAIL_URL, {})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertEqual(response.json()["sent_to"], self.owner.email)
        message = mail.outbox[0]
        self.assertEqual(message.to, [self.owner.email])
        self.assertIn("Your Zinwe quote ZWQ/", message.subject)
        self.assertIn(response.json()["public_url"], message.body)
        self.assertIn("zinwe@okbank", message.body)
        self.assertIn(SKYDO_PAGE, message.body)
        (name, content, mimetype), = message.attachments
        self.assertTrue(name.startswith("Zinwe-quote-ZWQ-") and name.endswith(".pdf"))
        self.assertEqual(mimetype, "application/pdf")
        self.assertTrue(content.startswith(b"%PDF"))

    def test_email_to_someone_else(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})
        mail.outbox.clear()
        response = self.post(EMAIL_URL, {"email": "accounts@acme.in"})
        self.assertEqual(response.json()["sent_to"], "accounts@acme.in")
        self.assertEqual(mail.outbox[0].to, ["accounts@acme.in"])
        self.assertIn("Asha Verma shared this quote with you", mail.outbox[0].body)
        bad = self.post(EMAIL_URL, {"email": "not-an-email"})
        self.assertEqual(bad.json(), {"email": ["Enter a valid email address."]})

    def test_renewal_quote_for_an_active_paid_plan(self) -> None:
        self.start_startups()
        doc = self.get(QUOTE_URL).json()
        self.assertEqual(doc["kind"], "renewal")
        self.assertIn("renewal", doc["description"])

    def test_requires_auth(self) -> None:
        self.use_token(None)
        self.assertEqual(self.get(QUOTE_URL).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.post(EMAIL_URL, {}).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_email_throttled(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})
        for _ in range(10):
            self.post(EMAIL_URL, {})
        self.assertEqual(self.post(EMAIL_URL, {}).status_code, status.HTTP_429_TOO_MANY_REQUESTS)
