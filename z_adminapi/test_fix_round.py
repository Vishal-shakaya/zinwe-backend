"""
Fix round 2026-10-17 (J8): staff check the receipt before approving, duplicates are flagged, block / reset
password / plan change tell the customer, delete keeps tax records, and the small helper endpoints.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.core import mail
from django.test import override_settings
from rest_framework import status

from z_analytics.models import Event
from z_auth.models import Announcement, Invoice, ManualPayment, Plan, Subscription
from z_notification.models import Notification
from z_notification.services import notify

from .templatetags.zinwe_admin import format_money
from .tests import PAYMENTS, SUBS, SubscriberFixture

HASH = "ab" * 32


class ApproveNeedsTheReceiptTests(SubscriberFixture):
    """J8-01: approving needs the amount and UTR staff read off the receipt; both are kept."""

    def test_both_fields_are_required(self) -> None:
        pay = self._pending_with_payment()
        for body in ({}, {"reference": "UTR9"}, {"amount": "971"}, {"reference": "  ", "amount": "971"},
                     {"reference": "UTR9", "amount": "0"}):
            response = self.client.post(f"{PAYMENTS}{pay.id}/approve/", body, format="json")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, body)
        pay.refresh_from_db()
        self.assertEqual(pay.status, ManualPayment.Status.SUBMITTED)
        self.assertEqual(pay.staff_reference, "")

    def test_typed_values_are_saved_with_who_checked(self) -> None:
        pay = self._pending_with_payment()
        response = self.client.post(f"{PAYMENTS}{pay.id}/approve/", {"reference": " 4321 8765 ", "amount": "971"},
                                    format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        pay.refresh_from_db()
        self.assertEqual((pay.staff_reference, pay.staff_amount, pay.staff_checked_by), ("43218765", Decimal("971"), self.staff))
        self.assertEqual(response.json()["invoice_number"], Invoice.objects.get(payment=pay).number)

    def test_a_reference_used_on_an_approved_payment_is_refused(self) -> None:
        first = self._pending_with_payment()
        self.client.post(f"{PAYMENTS}{first.id}/approve/", {"reference": "UTR777", "amount": "971"}, format="json")
        sub = first.subscription
        second = ManualPayment.objects.create(
            subscription=sub, method="upi", amount_inr=Decimal("971"), paid_on=date.today(), payer_name="Asha",
            is_renewal=True,
        )
        response = self.client.post(f"{PAYMENTS}{second.id}/approve/", {"reference": "utr777", "amount": "971"},
                                    format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already used for payment", response.json()["detail"])
        second.refresh_from_db()
        self.assertEqual((second.status, second.staff_reference), (ManualPayment.Status.SUBMITTED, ""))

    def test_due_total_is_the_amount_to_compare(self) -> None:
        pay = self._pending_with_payment()
        ManualPayment.objects.filter(pk=pay.pk).update(due={"total": "1145.78", "currency": "INR"})
        row = self.client.get(PAYMENTS).json()[0]
        self.assertEqual((row["due_total"], row["currency"]), ("1145.78", "INR"))

    def test_old_subscriber_endpoint_needs_them_too(self) -> None:
        self._pending_with_payment()
        url = f"{SUBS}{self.owner.id}/approve-payment/"
        self.assertEqual(self.client.post(url, {}, format="json").status_code, status.HTTP_400_BAD_REQUEST)
        ok = self.client.post(url, {"reference": "UTR1", "amount": "971"}, format="json")
        self.assertEqual(ok.status_code, status.HTTP_200_OK)


class DuplicateTests(SubscriberFixture):
    """J8-03: the same receipt file, or the same reference, as another payment is flagged."""

    def _payment(self, **extra) -> ManualPayment:  # type: ignore[no-untyped-def]
        sub = Subscription.objects.create(
            owner=self.owner, company=self.company, plan=self.paid,
            status=Subscription.Status.PENDING_PAYMENT, amount_inr=self.paid.price_inr_monthly,
        )
        return ManualPayment.objects.create(
            subscription=sub, method="upi", amount_inr=Decimal("971"), paid_on=date.today(), payer_name="Asha", **extra
        )

    def test_same_receipt_as_a_rejected_payment(self) -> None:
        old = self._payment(proof_sha256=HASH, status="rejected", rejection_reason="No UTR visible")
        new = self._payment(proof_sha256=HASH)
        dup = self.client.get(PAYMENTS, {"status": "submitted"}).json()[0]["duplicate_of"]
        self.assertEqual((dup["id"], dup["status"], dup["match"], dup["same_customer"]), (old.id, "rejected", "receipt", True))
        self.assertEqual(dup["rejection_reason"], "No UTR visible")
        self.assertNotEqual(dup["id"], new.id)

    def test_same_reference_prefers_the_approved_one(self) -> None:
        rejected = self._payment(reference="UTR5", status="rejected")
        approved = self._payment(staff_reference="UTR5", status="verified")
        self._payment(reference="utr5")
        dup = self.client.get(PAYMENTS, {"status": "submitted"}).json()[0]["duplicate_of"]
        self.assertEqual((dup["id"], dup["match"]), (approved.id, "reference"))
        self.assertNotEqual(dup["id"], rejected.id)

    def test_nothing_in_common(self) -> None:
        self._payment(proof_sha256=HASH, reference="A1", status="verified")
        self._payment(proof_sha256="cd" * 32, reference="B2")
        self.assertIsNone(self.client.get(PAYMENTS, {"status": "submitted"}).json()[0]["duplicate_of"])


class BlockTests(SubscriberFixture):
    """J8-04: block needs a reason, is recorded, and (by default) tells the customer."""

    def url(self) -> str:
        return f"{SUBS}{self.owner.id}/block/"

    def test_reason_is_required(self) -> None:
        response = self.client.post(self.url(), {"reason": " "}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_active)

    def test_block_emails_the_customer_and_records_why(self) -> None:
        response = self.client.post(self.url(), {"reason": "Payment reversed by the bank"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertEqual((email.to, email.subject), ([self.owner.email], "Your Zinwe account is paused"))
        self.assertIn("Payment reversed by the bank", email.body)
        self.assertIn("hello@zinwe.io", email.body)
        blocked = response.json()["blocked"]
        self.assertEqual((blocked["reason"], blocked["by"], blocked["emailed"]),
                         ("Payment reversed by the bank", self.staff.email, True))

    def test_block_without_email(self) -> None:
        self.client.post(self.url(), {"reason": "Fraud check", "email_customer": False}, format="json")
        self.assertEqual(len(mail.outbox), 0)
        self.assertTrue(Event.objects.filter(name="admin.customer_blocked").exists())

    def test_unblock_clears_the_record(self) -> None:
        self.client.post(self.url(), {"reason": "x", "email_customer": False}, format="json")
        body = self.client.post(f"{SUBS}{self.owner.id}/unblock/").json()
        self.assertIsNone(body["blocked"])
        self.assertTrue(body["is_active"])

    def test_unblock_tells_the_customer(self) -> None:
        from django.core import mail

        self.client.post(self.url(), {"reason": "x", "email_customer": False}, format="json")
        mail.outbox.clear()
        self.client.post(f"{SUBS}{self.owner.id}/unblock/")
        self.assertEqual([m.subject for m in mail.outbox], ["Your Zinwe account is active again"])
        self.assertIn("/login", mail.outbox[0].body)


class PasswordTests(SubscriberFixture):
    """J8-05: the safe default emails a reset code; a password set by hand is announced to the customer."""

    def test_send_reset_code(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"{SUBS}{self.owner.id}/send-password-reset/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertEqual(response.json(), {"sent": True, "email": self.owner.email})
        self.assertEqual(Notification.objects.get(recipient=self.owner.email).template, "otp_password_reset")
        self.assertTrue(self.owner.check_password("pw-12345678"))  # nothing changed until they use the code

    def test_second_reset_within_a_minute_is_explained(self) -> None:
        self.client.post(f"{SUBS}{self.owner.id}/send-password-reset/")
        response = self.client.post(f"{SUBS}{self.owner.id}/send-password-reset/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("moments ago", response.json()["detail"])

    def test_blocked_or_unfinished_accounts_get_no_code(self) -> None:
        self.owner.is_active = False
        self.owner.save()
        response = self.client.post(f"{SUBS}{self.owner.id}/send-password-reset/")
        self.assertIn("blocked", response.json()["detail"])
        self.owner.email_verified_at = None
        self.owner.save()
        response = self.client.post(f"{SUBS}{self.owner.id}/send-password-reset/")
        self.assertIn("haven't finished signing up", response.json()["detail"])

    def test_manual_password_emails_the_customer(self) -> None:
        response = self.client.post(f"{SUBS}{self.owner.id}/set-password/", {"password": "Fresh-Passphrase-42"},
                                    format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Your Zinwe password was changed")
        self.assertNotIn("Fresh-Passphrase-42", mail.outbox[0].body)

    def test_manual_password_without_email(self) -> None:
        self.client.post(f"{SUBS}{self.owner.id}/set-password/",
                         {"password": "Fresh-Passphrase-42", "email_customer": False}, format="json")
        self.assertEqual(len(mail.outbox), 0)


class DeleteKeepsTaxRecordsTests(SubscriberFixture):
    """J8-12: a customer with a tax invoice can't be deleted (invoices are kept 8 years)."""

    def test_refused_with_an_invoice(self) -> None:
        pay = self._pending_with_payment()
        self.client.post(f"{PAYMENTS}{pay.id}/approve/", {"reference": "UTR1", "amount": "971"}, format="json")
        number = Invoice.objects.get(payment=pay).number
        response = self.client.delete(f"{SUBS}{self.owner.id}/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(number, response.json()["detail"])
        self.assertIn("Block it instead", response.json()["detail"])
        self.assertTrue(ManualPayment.objects.filter(pk=pay.pk).exists())


class ChangePlanEmailTests(SubscriberFixture):
    """J8-11: the customer can be told about a plan change."""

    def test_email_customer(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"{SUBS}{self.owner.id}/change-plan/",
                                        {"plan": Plan.STARTUPS, "email_customer": True}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, f"Your Zinwe plan is now {self.paid.name}")
        self.assertIn("/app/settings/billing", mail.outbox[0].body)

    def test_no_email_by_default(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(f"{SUBS}{self.owner.id}/change-plan/", {"plan": Plan.STARTUPS}, format="json")
        self.assertEqual(len(mail.outbox), 0)


class HelperEndpointTests(SubscriberFixture):
    def test_badges(self) -> None:
        self._pending_with_payment()
        body = self.client.get("/api/admin/badges/").json()
        self.assertEqual(body, {"payments_waiting": 1, "support_waiting": 0, "forms_new": 0})
        self.assertIn("support_waiting", self.client.get("/api/admin/metrics/").json()["totals"])

    @override_settings(ZINWE_INVOICE_SELLER={"gstin": ""})
    def test_billing_settings_without_gstin(self) -> None:
        self.assertEqual(self.client.get("/api/admin/billing-settings/").json(), {"gst_charged": False, "seller_gstin": ""})

    @override_settings(ZINWE_INVOICE_SELLER={"gstin": "07ABCDE1234F1Z5"})
    def test_billing_settings_with_gstin(self) -> None:
        self.assertTrue(self.client.get("/api/admin/billing-settings/").json()["gst_charged"])

    def test_release_reach(self) -> None:
        self.assertEqual(self.client.get("/api/admin/releases/reach/", {"version": "9.9.9"}).json(), {"older": 0, "total": 0})
        bad = self.client.get("/api/admin/releases/reach/", {"version": "v1.0 beta"})
        self.assertEqual(bad.status_code, status.HTTP_400_BAD_REQUEST)

    def test_release_is_optional_unless_ticked(self) -> None:
        from .serializers import PublishReleaseSerializer

        data = PublishReleaseSerializer(data={"version": "1.2.3", "download_url": "https://x.test/a.msi", "sha256": HASH})
        self.assertTrue(data.is_valid(), data.errors)
        self.assertFalse(data.validated_data["required"])

    def test_announcement_reach(self) -> None:
        self.make_owner("free@acme.in")
        body = self.client.get("/api/admin/announcements/reach/", {"audience": "everyone"}).json()
        self.assertEqual(body, {"customers": 2, "total": 2})
        paid = self.client.get("/api/admin/announcements/reach/", {"audience": "paid"}).json()
        self.assertEqual(paid["customers"], 0)
        bad = self.client.get("/api/admin/announcements/reach/", {"audience": "nobody"})
        self.assertEqual(bad.status_code, status.HTTP_400_BAD_REQUEST)

    def test_announcement_link_can_be_mailto(self) -> None:
        response = self.client.post("/api/admin/announcements/", {
            "title": "Talk to us", "cta_label": "Email us", "cta_href": "mailto:hello@zinwe.io",
            "kind": Announcement.Kind.NEWS,
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        bad = self.client.post("/api/admin/announcements/", {
            "title": "x", "cta_label": "Go", "cta_href": "javascript:alert(1)", "kind": Announcement.Kind.NEWS,
        }, format="json")
        self.assertEqual(set(bad.json()), {"cta_href"})


@override_settings(ZINWE_ADMIN_URL="https://admin.zinwe.test")
class StaffPaymentEmailTests(SubscriberFixture):
    """J8-09 / J8-13: the "payment to check" email links straight to the review, no Django admin, honest tax."""

    def _send(self, due: dict) -> None:  # type: ignore[type-arg]
        pay = self._pending_with_payment()
        pay.reference = ""
        pay.due = due
        pay.save()
        sub = pay.subscription
        notify("email", "team@zinwe.test", "payment_received_team",
               {"payment": pay, "subscription": sub, "owner": self.owner, "company": self.company, "plan": sub.plan})
        self.pay = pay

    def test_link_and_wording(self) -> None:
        self._send({"total": "971.00", "amount": "971.00", "tax_total": "0.00", "taxes": [], "currency": "INR"})
        email = mail.outbox[0]
        self.assertIn(f"https://admin.zinwe.test/subscribers?review={self.pay.id}", email.body)
        self.assertIn(f'href="https://admin.zinwe.test/subscribers?review={self.pay.id}"', email.alternatives[0][0])
        self.assertNotIn("Django", email.body)
        self.assertNotIn("+ tax", email.body)
        self.assertIn("₹971 (no GST)", email.body)
        self.assertIn("read it off the receipt", email.body)
        self.assertEqual(email.subject, "Payment to check: Acme, ₹971 (UPI)")

    def test_gst_spelled_out(self) -> None:
        self._send({"total": "1145.78", "amount": "971.00", "tax_total": "174.78", "currency": "INR",
                    "taxes": [{"label": "IGST 18%", "rate": "18", "amount": "174.78"}]})
        self.assertIn("₹1,145.78 (₹971 + IGST 18% ₹174.78)", mail.outbox[0].body)


class MoneyFormatTests(SubscriberFixture):
    def test_formats(self) -> None:
        self.assertEqual(format_money("1234567.5"), "₹12,34,567.50")
        self.assertEqual(format_money("971.00"), "₹971")
        self.assertEqual(format_money("144", "USD"), "$144.00")
