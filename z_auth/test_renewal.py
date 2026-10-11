"""Renewals: paying for the next month while a paid plan is active, late renewals, payment history."""
from __future__ import annotations

from datetime import timedelta

from django.core import mail
from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token

from ._testing import PortalTestCase
from .billing import add_months, verify_payment
from .models import ManualPayment, PlanActivation, Subscription, User

SUB_URL = "/api/auth/subscription/"
PAY_URL = "/api/auth/subscription/payment/"
HISTORY_URL = "/api/auth/subscription/payments/"
ME = "/api/auth/me/"


def payment_form(**overrides):  # type: ignore[no-untyped-def]
    return {
        "method": "upi", "amount_inr": "999", "reference": "UTR-NEW",
        "paid_on": timezone.localdate().isoformat(), "payer_name": "Asha Verma", **overrides,
    }


class RenewalTests(PortalTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.owner = self.make_owner()
        self.make_company(self.owner)
        self.staff = User.objects.create_superuser("ops@zinwe.local", "ops-12345678")
        # Startups, first payment approved: active for a month.
        self.post(SUB_URL, {"plan": "startups"})
        self.post(PAY_URL, payment_form(), fmt="multipart")
        first = ManualPayment.objects.get()
        verify_payment(first.pk, self.staff)
        self.first = ManualPayment.objects.get(pk=first.pk)
        self.sub = Subscription.objects.get()
        mail.outbox.clear()

    def info(self) -> dict:
        return self.get(SUB_URL).json()["subscription"]

    def renew(self, **overrides):  # type: ignore[no-untyped-def]
        return self.post(PAY_URL, payment_form(**{"reference": "UTR-RENEW", **overrides}), fmt="multipart")

    def test_first_payment_records_its_period(self) -> None:
        self.assertFalse(self.first.is_renewal)
        self.assertEqual(self.first.covers_until, self.sub.ends_at)
        self.assertLess(abs((self.first.covers_from - timezone.now()).total_seconds()), 60)

    def test_active_plan_shows_days_left_and_renewal_preview(self) -> None:
        body = self.info()
        self.assertIn(body["days_left"], (30, 31, 28, 29))
        self.assertTrue(body["can_renew"])
        self.assertEqual(body["renewal_period"]["from"], body["ends_at"])  # extends from the end: no days lost

    def test_renew_early_extends_from_current_end(self) -> None:
        old_end = self.sub.ends_at
        response = self.renew()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        self.assertTrue(response.json()["is_renewal"])
        # Still active and the dashboard stays open while it's checked.
        self.assertEqual(self.get(ME).json()["onboarding"]["step"], "complete")
        self.assertFalse(self.info()["can_renew"])  # one payment under review at a time
        self.assertEqual(mail.outbox[0].subject.split(":")[0], "Renewal to check")
        self.assertIn("Renewal of an active plan", mail.outbox[0].body)

        mail.outbox.clear()
        renewal = ManualPayment.objects.get(is_renewal=True)
        with self.captureOnCommitCallbacks(execute=True):
            verify_payment(renewal.pk, self.staff)
        self.sub.refresh_from_db()
        renewal.refresh_from_db()
        self.assertEqual(self.sub.status, "active")
        self.assertEqual(self.sub.ends_at, add_months(old_end, 1))
        self.assertEqual((renewal.covers_from, renewal.covers_until), (old_end, add_months(old_end, 1)))
        self.assertEqual(PlanActivation.objects.get(pk=self.sub.activation_id).ends_at, self.sub.ends_at)
        self.assertEqual(mail.outbox[0].subject, "Your Zinwe Startups plan is renewed")
        self.assertIn("extended by a month", mail.outbox[0].body)
        self.assertTrue(self.info()["can_renew"])

    def test_second_renewal_while_one_is_checked_is_refused(self) -> None:
        self.renew()
        response = self.renew(reference="UTR-AGAIN")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(ManualPayment.objects.filter(is_renewal=True).count(), 1)

    def test_rejected_renewal_keeps_the_plan(self) -> None:
        from .billing import reject_payment

        self.renew()
        renewal = ManualPayment.objects.get(is_renewal=True)
        with self.captureOnCommitCallbacks(execute=True):
            reject_payment(renewal.pk, self.staff, "UTR not found")
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, "active")
        self.assertTrue(self.info()["can_renew"])  # they can try again

    def test_renewal_approved_after_the_plan_lapsed_restarts_it(self) -> None:
        self.renew()
        Subscription.objects.filter(pk=self.sub.pk).update(ends_at=timezone.now() - timedelta(days=2))
        self.assertEqual(self.info()["status"], "expired")  # lapsed while the payment waited
        renewal = ManualPayment.objects.get(is_renewal=True)
        verify_payment(renewal.pk, self.staff)
        self.sub.refresh_from_db()
        renewal.refresh_from_db()
        self.assertEqual(self.sub.status, "active")
        self.assertLess(abs((renewal.covers_from - timezone.now()).total_seconds()), 60)
        self.assertEqual(self.sub.ends_at, renewal.covers_until)
        self.assertTrue(PlanActivation.objects.filter(user=self.owner, is_active=True).exists())

    def test_free_plan_has_nothing_to_renew(self) -> None:
        other = self.make_owner("free@acme.in")
        self.make_company(other)
        self.post(SUB_URL, {"plan": "free"})
        self.assertFalse(self.info()["can_renew"])
        self.assertIsNone(self.info()["days_left"])
        self.assertEqual(self.post(PAY_URL, payment_form(), fmt="multipart").status_code, status.HTTP_409_CONFLICT)

    def test_payment_history(self) -> None:
        self.renew()
        body = self.get(HISTORY_URL).json()
        self.assertEqual([(p["reference"], p["is_renewal"]) for p in body], [("UTR-RENEW", True), ("UTR-NEW", False)])
        self.assertEqual(body[1]["plan_name"], "Startups")
        self.assertIsNotNone(body[1]["covers_until"])
        # Own payments only.
        stranger = User.objects.create_user("x@acme.in", "pw-12345678")
        self.use_token(Token.objects.create(user=stranger).key)
        self.assertEqual(self.get(HISTORY_URL).json(), [])

    def test_admin_sees_days_left_type_and_preview(self) -> None:
        old_end = self.sub.ends_at
        self.renew()
        self.use_token(Token.objects.create(user=self.staff).key)
        row = next(r for r in self.get("/api/admin/subscribers/").json() if r["id"] == self.owner.id)
        self.assertIsNotNone(row["subscription"]["days_left"])
        self.assertTrue(row["subscription"]["renewable"])
        waiting = row["payment"]
        self.assertTrue(waiting["is_renewal"])
        self.assertEqual(waiting["cycle"], 2)
        self.assertEqual(waiting["would_cover"]["from"], old_end.isoformat().replace("+00:00", "Z"))
        approved = row["payments"][1]
        self.assertEqual((approved["is_renewal"], approved["cycle"]), (False, 1))
        self.assertIsNotNone(approved["covers_from"])
