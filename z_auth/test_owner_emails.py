"""
Fix round 2026-10-17 (user-journey test), owner side: the emails around money and passwords, the upgrade
preview, UPI link, plans API additions, paused accounts, the dashboard tour, and staff receipt checks.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework import status

from z_devices.models import Device

from . import billing
from ._testing import PortalTestCase, last_code
from .invoices import amount_in_words, invoice_for_payment, payment_line
from .models import ManualPayment, Subscription, User
from .test_portal import MediaMixin

SUB_URL = "/api/auth/subscription/"
PAY_URL = "/api/auth/subscription/payment/"
PLANS_URL = "/api/auth/plans/"
ME = "/api/auth/me/"
TOUR_URL = "/api/auth/tour/done/"
SELLER = {"name": "SKYTECHNO", "address": "New Delhi, India", "email": "hello@zinwe.io", "gstin": "07AAACS1234K1Z5"}
UPI = {
    "upi_id": "zinwe@okbank", "payee_name": "SKYTECHNO", "bank_name": "", "account_type": "", "account_number": "",
    "ifsc": "", "paypal_url": "", "note": "",
}


def upgrade_url(code: str) -> str:
    return f"/api/auth/subscription/upgrade/{code}/"


PNG_BYTES = bytes([0x89]) + b"PNG" + bytes([13, 10, 26, 10]) + b"r" * 64


def receipt(name: str = "receipt.png", content: bytes = PNG_BYTES) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content, content_type="image/png")


def payment_form(**overrides):  # type: ignore[no-untyped-def]
    """A payment with only a receipt (no reference typed), like the payment page sends."""
    return {
        "method": "upi", "amount_inr": "971", "reference": "", "proof": receipt(),
        "paid_on": timezone.localdate().isoformat(), "payer_name": "Asha Verma", "accept_terms": "true",
        **overrides,
    }


class OwnerBase(MediaMixin, PortalTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.owner = self.make_owner()
        self.company = self.make_company(self.owner)
        self.staff = User.objects.create_superuser("ops@zinwe.local", "ops-12345678")

    def verify(self, payment: ManualPayment, **staff: object) -> ManualPayment:
        with self.captureOnCommitCallbacks(execute=True):
            billing.verify_payment(payment.pk, self.staff, **staff)
        payment.refresh_from_db()
        return payment

    def reject(self, payment: ManualPayment, reason: str = "The receipt is for a different amount.") -> None:
        with self.captureOnCommitCallbacks(execute=True):
            billing.reject_payment(payment.pk, self.staff, reason)

    def start_startups(self) -> Subscription:
        self.post(SUB_URL, {"plan": "startups"})
        self.post(PAY_URL, payment_form(reference="UTR-1"), fmt="multipart")
        self.verify(ManualPayment.objects.get())
        mail.outbox.clear()
        return Subscription.objects.get()


# --- Approval / plan-active email (J3-03, J3-20, J8-15, J5-19) -----------------------------------


class ApprovalEmailTests(OwnerBase):
    def test_first_payment_email_has_amount_period_invoice_pdf_and_links(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})
        self.post(PAY_URL, payment_form(), fmt="multipart")
        mail.outbox.clear()
        payment = self.verify(ManualPayment.objects.get(), staff_reference="UTR998877", staff_amount=Decimal("971"))
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.subject, "Your Zinwe Startups plan is active")
        invoice = invoice_for_payment(payment)
        self.assertIn("Payment received: ₹971.00", sent.body)
        self.assertIn(f"Invoice: {invoice.number} (the PDF is attached)", sent.body)
        self.assertIn(f"{settings.ZINWE_APP_URL}/app/settings/billing", sent.body)
        # No PC yet: the next step is adding the first person (not "install the agent").
        self.assertIn("add your first person", sent.body)
        self.assertIn(f"{settings.ZINWE_APP_URL}/app/team", sent.body)
        self.assertNotIn("agent", sent.body.lower())
        name, content, mimetype = sent.attachments[0]
        self.assertEqual(name, f"Zinwe-invoice-{invoice.number.replace('/', '-')}.pdf")
        self.assertEqual(mimetype, "application/pdf")
        self.assertTrue(content.startswith(b"%PDF"))
        html = sent.alternatives[0][0]
        self.assertIn('href="http', html)
        self.assertIn("Add your first person", html)
        # Staff's receipt check is stored and goes on the invoice.
        self.assertEqual((payment.staff_reference, payment.staff_amount), ("UTR998877", Decimal("971.00")))
        self.assertEqual(payment.staff_checked_by, self.staff)
        self.assertEqual(invoice.data["payment"]["reference"], "UTR998877")

    def test_owner_with_pcs_gets_a_dashboard_button_instead(self) -> None:
        Device.objects.create(owner=self.owner, machine_id="m1", hostname="PC-1", token_hash="t" * 64)
        self.post(SUB_URL, {"plan": "startups"})
        self.post(PAY_URL, payment_form(), fmt="multipart")
        mail.outbox.clear()
        self.verify(ManualPayment.objects.get())
        body = mail.outbox[0].body
        self.assertNotIn("add your first person", body)
        self.assertIn(f"Open my dashboard: {settings.ZINWE_APP_URL}/app", body)

    def test_upgrade_email_says_days_carried_over(self) -> None:
        self.start_startups()
        self.post(PAY_URL, payment_form(upgrade_plan="teams", amount_inr="1799"), fmt="multipart")
        mail.outbox.clear()
        payment = self.verify(ManualPayment.objects.get(upgrade_plan__code="teams"))
        sent = mail.outbox[0]
        self.assertEqual(sent.subject, "You're now on Zinwe Teams")
        self.assertIn("covers up to 25 PCs", sent.body)
        self.assertRegex(sent.body, r"includes (29|30|31) paid days carried over")
        self.assertIn(f"paid until {timezone.localtime(payment.covers_until).strftime('%d %B %Y').lstrip('0')}", sent.body)

    def test_free_plan_email_links_to_the_dashboard(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(SUB_URL, {"plan": "free"}, format="json")
        sent = mail.outbox[-1]
        self.assertEqual(sent.subject, "Your Zinwe Free forever plan is active")
        self.assertIn(f"{settings.ZINWE_APP_URL}/app/team", sent.body)
        self.assertEqual(sent.attachments, [])


# --- Receipt and rejection emails (J3-04, J3-05, J3-12, J8-14) ------------------------------------


class ReceiptAndRejectionTests(OwnerBase):
    def test_rejection_email_has_reason_amount_link_and_no_empty_reference(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})
        self.post(PAY_URL, payment_form(reference=""), fmt="multipart")
        mail.outbox.clear()
        self.reject(ManualPayment.objects.get())
        sent = mail.outbox[0]
        self.assertIn("₹971.00", sent.body)
        self.assertNotIn("reference )", sent.body)
        self.assertNotIn("reference", sent.body)
        self.assertIn("Reason: The receipt is for a different amount.", sent.body)
        self.assertIn(f"{settings.ZINWE_APP_URL}/app/settings/billing", sent.body)
        self.assertIn("starts once we've confirmed a payment", sent.body)
        self.assertIn("Send the receipt again", sent.alternatives[0][0])

    def test_rejected_renewal_says_the_plan_keeps_running(self) -> None:
        self.start_startups()
        self.post(PAY_URL, payment_form(reference="UTR-R"), fmt="multipart")
        mail.outbox.clear()
        self.reject(ManualPayment.objects.get(is_renewal=True))
        body = mail.outbox[0].body
        self.assertIn("(reference UTR-R)", body)
        self.assertIn("Your Startups plan keeps running as before.", body)
        self.assertNotIn("waiting for payment", body)

    def test_receipt_email_for_an_upgrade(self) -> None:
        self.start_startups()
        self.post(PAY_URL, payment_form(upgrade_plan="teams", amount_inr="1799"), fmt="multipart")
        owner_mail = [m for m in mail.outbox if m.to == ["owner@acme.in"]]
        self.assertEqual(len(owner_mail), 1)
        self.assertIn("for the upgrade to Teams", owner_mail[0].body)
        self.assertIn("current plan keeps running", owner_mail[0].body)


# --- Invoice details (J3-12, J3-25) ---------------------------------------------------------------


class InvoiceTextTests(OwnerBase):
    def test_payment_line_skips_empty_parts(self) -> None:
        self.assertEqual(payment_line({"method": "UPI", "reference": "", "payer": "Asha"}), "UPI - paid by Asha")
        self.assertEqual(payment_line({"method": "UPI", "reference": " 42 ", "payer": ""}), "UPI - reference 42")

    def test_us_dollars_keeps_its_capitals(self) -> None:
        self.assertEqual(amount_in_words(Decimal("144.00"), "USD"), "US dollars one hundred forty-four only")
        self.assertTrue(amount_in_words(Decimal("971.00"), "INR").startswith("Rupees nine hundred"))


# --- Staff receipt checks: hash + duplicates --------------------------------------------------------


class DuplicateTests(OwnerBase):
    def test_same_receipt_or_same_reference_is_a_duplicate(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})
        same = PNG_BYTES + b"same-receipt" * 10
        self.post(PAY_URL, payment_form(proof=receipt("a.png", same)), fmt="multipart")
        first = ManualPayment.objects.get()
        self.assertEqual(len(first.proof_sha256), 64)
        self.assertIsNone(billing.duplicate_of(first))
        self.reject(first)
        self.post(PAY_URL, payment_form(proof=receipt("b.png", same)), fmt="multipart")
        second = ManualPayment.objects.exclude(pk=first.pk).get()
        self.assertEqual(second.proof_sha256, first.proof_sha256)
        self.assertEqual(billing.duplicate_of(second), first)

        ManualPayment.objects.filter(pk=first.pk).update(proof_sha256="", staff_reference="UTR555")
        second.staff_reference = "utr555"
        second.proof_sha256 = ""
        self.assertEqual(billing.duplicate_of(second), ManualPayment.objects.get(pk=first.pk))


# --- Upgrade preview + UPI link (J3-07, J3-08) -------------------------------------------------------


@override_settings(ZINWE_PAYMENT_INSTRUCTIONS=UPI)
class UpgradePreviewTests(OwnerBase):
    def test_preview_from_a_paid_plan(self) -> None:
        sub = self.start_startups()
        response = self.get(upgrade_url("teams"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        body = response.json()
        self.assertEqual(
            set(body), {"quote", "covers_from", "covers_until", "carried_days", "upi_qr_svg", "upi_uri"}
        )
        self.assertEqual(body["quote"]["plan_code"], "teams")
        self.assertEqual(body["quote"]["amount"], "1799.00")
        self.assertEqual(body["covers_from"], timezone.localdate().isoformat())
        self.assertIn(body["carried_days"], (29, 30, 31))
        start, until = billing.upgrade_period(sub)
        self.assertEqual(body["covers_until"], timezone.localdate(until).isoformat())
        self.assertTrue(body["upi_uri"].startswith("upi://pay?pa=zinwe%40okbank"))
        self.assertIn(f"am={body['quote']['total']}", body["upi_uri"])
        self.assertIn("<svg", body["upi_qr_svg"])

    def test_preview_from_free_carries_nothing(self) -> None:
        self.post(SUB_URL, {"plan": "free"})
        body = self.get(upgrade_url("startups")).json()
        self.assertEqual(body["carried_days"], 0)

    def test_not_allowed_is_409_and_unknown_plan_404(self) -> None:
        self.assertEqual(self.get(upgrade_url("teams")).status_code, status.HTTP_409_CONFLICT)  # no plan yet
        self.start_startups()
        self.assertEqual(self.get(upgrade_url("startups")).status_code, status.HTTP_409_CONFLICT)  # same plan
        self.assertEqual(self.get(upgrade_url("free")).status_code, status.HTTP_409_CONFLICT)  # lower
        self.assertEqual(self.get(upgrade_url("nope")).status_code, status.HTTP_404_NOT_FOUND)
        self.post(PAY_URL, payment_form(upgrade_plan="teams", amount_inr="1799"), fmt="multipart")
        under_review = self.get(upgrade_url("businesses"))
        self.assertEqual(under_review.status_code, status.HTTP_409_CONFLICT)  # J3-01: one payment at a time
        self.assertIn("being reviewed", under_review.json()["detail"])

    def test_needs_login(self) -> None:
        self.use_token(None)
        self.assertEqual(self.get(upgrade_url("teams")).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_subscription_instructions_carry_the_upi_link(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})
        instructions = self.get(SUB_URL).json()["instructions"]
        self.assertTrue(instructions["upi_uri"].startswith("upi://pay?pa=zinwe%40okbank&pn=SKYTECHNO&am=971.00"))


# --- Plans API (history_days, gst_charged) ------------------------------------------------------------


class PlansApiTests(PortalTestCase):
    @override_settings(ZINWE_INVOICE_SELLER=SELLER)
    def test_gst_charged_with_a_seller_gstin_for_india_and_visitors(self) -> None:
        plans = self.get(PLANS_URL).json()
        self.assertTrue(all(p["gst_charged"] for p in plans))
        owner = self.make_owner()
        self.make_company(owner, country="United States", state="California")
        self.assertFalse(any(p["gst_charged"] for p in self.get(PLANS_URL).json()))

    @override_settings(ZINWE_INVOICE_SELLER={**SELLER, "gstin": ""})
    def test_no_gst_without_a_seller_gstin(self) -> None:
        self.assertFalse(any(p["gst_charged"] for p in self.get(PLANS_URL).json()))

    def test_history_days(self) -> None:
        plans = {p["code"]: p["history_days"] for p in self.get(PLANS_URL).json()}
        self.assertEqual(plans["teams"], 180)


# --- Accounts: paused, password changed, welcome, tour ------------------------------------------------


class AccountTests(PortalTestCase):
    def test_paused_account_code_login_is_403(self) -> None:
        user = self.make_owner("asha@acme.in", login=False)
        self.post("/api/auth/login/otp/request/", {"email": "asha@acme.in"})
        code = last_code("asha@acme.in")
        User.objects.filter(pk=user.pk).update(is_active=False)
        response = self.post("/api/auth/login/otp/verify/", {"email": "asha@acme.in", "code": code})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            response.json(),
            {"code": "account_paused", "detail": "Your Zinwe account is paused. Write to hello@zinwe.io and we'll help."},
        )

    def test_password_change_sends_a_notice(self) -> None:
        self.make_owner("asha@acme.in")
        mail.outbox.clear()
        response = self.post(
            "/api/auth/password/change/",
            {"current_password": "s3cure-Passphrase!", "new_password": "an0ther-Long-phrase"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        sent = mail.outbox[-1]
        self.assertEqual(sent.subject, "Your Zinwe password was changed")
        self.assertIn("changed from your dashboard", sent.body)
        self.assertIn(f"{settings.ZINWE_APP_URL}/forgot-password", sent.body)

    def test_password_reset_sends_a_notice(self) -> None:
        self.make_owner("asha@acme.in", login=False)
        self.post("/api/auth/password/forgot/", {"email": "asha@acme.in"})
        code = last_code("asha@acme.in")
        mail.outbox.clear()
        response = self.post(
            "/api/auth/password/reset/",
            {"email": "asha@acme.in", "code": code, "new_password": "an0ther-Long-phrase"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertEqual([m.subject for m in mail.outbox], ["Your Zinwe password was changed"])
        self.assertIn("reset with an emailed code", mail.outbox[0].body)

    def test_welcome_email_has_a_dashboard_button(self) -> None:
        self.signup_and_verify()
        welcome = [m for m in mail.outbox if m.subject == "Welcome to Zinwe"][0]
        self.assertIn(f"Open my dashboard: {settings.ZINWE_APP_URL}/app", welcome.body)
        self.assertIn(f'href="{settings.ZINWE_APP_URL}/app"', welcome.alternatives[0][0])

    def test_tour_is_once_per_account(self) -> None:
        self.make_owner()
        self.assertFalse(self.get(ME).json()["tour_done"])
        response = self.post(TOUR_URL)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertTrue(self.get(ME).json()["tour_done"])
        first = User.objects.get().tour_done_at
        self.assertEqual(self.post(TOUR_URL).status_code, status.HTTP_204_NO_CONTENT)  # idempotent
        self.assertEqual(User.objects.get().tour_done_at, first)

    def test_tour_needs_login(self) -> None:
        self.assertEqual(self.post(TOUR_URL).status_code, status.HTTP_401_UNAUTHORIZED)


class SettingsTests(PortalTestCase):
    def test_agent_download_url_follows_the_app_url(self) -> None:
        self.assertTrue(settings.ZINWE_AGENT_DOWNLOAD_URL)
        if "ZINWE_AGENT_DOWNLOAD_URL" not in __import__("os").environ:
            self.assertEqual(settings.ZINWE_AGENT_DOWNLOAD_URL, f"{settings.ZINWE_APP_URL}/download/ZinweAgent.msi")

    def test_legal_version(self) -> None:
        self.assertEqual(settings.ZINWE_LEGAL_VERSION, "2026-10-17")


class RenewalReminderCopyTests(OwnerBase):
    def test_reminder_says_pay_and_upload_the_receipt(self) -> None:
        from .renewals import send_due_reminders

        sub = self.start_startups()
        Subscription.objects.filter(pk=sub.pk).update(ends_at=timezone.now() + timedelta(days=6, hours=12))
        send_due_reminders()
        body = mail.outbox[-1].body
        self.assertIn("upload the receipt on Billing", body)
        self.assertNotIn("payment reference", body)


from .test_upgrade import _UpgradeBase  # noqa: E402


class TeamSizeSuggestionTests(_UpgradeBase):
    """J1-02: at the PC limit, the upgrade bar suggests a plan big enough for the team size given at sign-up."""

    def test_eleven_to_twenty_five_people_get_teams(self) -> None:
        from .announcements import announcements_for, team_size_upper
        from .entitlements import entitlements_for

        company = self.owner.owned_company
        company.team_size = "11-25"
        company.save()
        self.assertEqual(team_size_upper(self.owner), 25)
        ent = entitlements_for(self.owner)
        from unittest import mock

        with mock.patch("z_auth.announcements.devices_used", return_value=ent.max_devices):
            bar = next(a for a in announcements_for(self.owner) if a["kind"] == "upgrade")
        self.assertEqual(bar["cta_href"], "/app/upgrade?plan=teams")
