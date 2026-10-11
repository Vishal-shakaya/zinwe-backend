"""Dashboard upgrades: paying to move up a plan (POST subscription/payment/ with upgrade_plan)."""
from __future__ import annotations

from datetime import timedelta

from django.core import mail
from django.utils import timezone
from rest_framework import status

from ._testing import PortalTestCase
from .billing import add_months, verify_payment
from .models import ManualPayment, PlanActivation, Subscription, User

SUB_URL = "/api/auth/subscription/"
PAY_URL = "/api/auth/subscription/payment/"
ME = "/api/auth/me/"


def payment_form(**overrides):  # type: ignore[no-untyped-def]
    return {
        "method": "upi", "amount_inr": "2999", "reference": "UTR-UP",
        "paid_on": timezone.localdate().isoformat(), "payer_name": "Asha Verma", **overrides,
    }


class UpgradeTests(PortalTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.owner = self.make_owner()
        self.make_company(self.owner)
        self.staff = User.objects.create_superuser("ops@zinwe.local", "ops-12345678")

    def start_startups(self) -> Subscription:
        """Startups with its first payment approved: active for a month."""
        self.post(SUB_URL, {"plan": "startups"})
        self.post(PAY_URL, payment_form(amount_inr="999", reference="UTR-1"), fmt="multipart")
        verify_payment(ManualPayment.objects.get().pk, self.staff)
        mail.outbox.clear()
        return Subscription.objects.get()

    def upgrade(self, plan: str = "businesses"):  # type: ignore[no-untyped-def]
        return self.post(PAY_URL, payment_form(upgrade_plan=plan), fmt="multipart")

    def test_upgrade_waits_for_approval_then_switches_with_days_carried_over(self) -> None:
        sub = self.start_startups()
        old_end = sub.ends_at
        response = self.upgrade()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        self.assertEqual(response.json()["upgrade_plan_name"], "Businesses")
        # The current plan keeps running and the dashboard stays open while it's checked.
        self.assertEqual(self.get(ME).json()["onboarding"]["step"], "complete")
        sub.refresh_from_db()
        self.assertEqual(sub.plan.code, "startups")
        self.assertTrue(mail.outbox[0].subject.startswith("Upgrade to check"))

        payment = ManualPayment.objects.get(upgrade_plan__code="businesses")
        verify_payment(payment.pk, self.staff)
        sub.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual((sub.plan.code, sub.status, str(sub.amount_inr)), ("businesses", "active", "2999.00"))
        carried = old_end - payment.covers_from
        self.assertLess(abs((sub.ends_at - (add_months(payment.covers_from, 1) + carried)).total_seconds()), 5)
        active = PlanActivation.objects.get(user=self.owner, is_active=True)
        self.assertEqual(active.plan.code, "businesses")

    def test_upgrade_from_free_plan(self) -> None:
        self.post(SUB_URL, {"plan": "free"})
        response = self.upgrade("startups")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        verify_payment(ManualPayment.objects.get().pk, self.staff)
        sub = Subscription.objects.get()
        self.assertEqual((sub.plan.code, sub.is_trial), ("startups", False))
        self.assertLess(abs((sub.ends_at - add_months(sub.starts_at, 1)).total_seconds()), 5)  # nothing to carry

    def test_same_or_lower_plan_is_refused(self) -> None:
        self.start_startups()
        for plan in ("startups", "free"):
            with self.subTest(plan=plan):
                response = self.upgrade(plan)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("upgrade_plan", response.json())
        self.assertEqual(ManualPayment.objects.count(), 1)

    def test_unknown_plan_is_a_field_error(self) -> None:
        self.start_startups()
        response = self.upgrade("platinum")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["upgrade_plan"], ["That plan isn't available."])

    def test_one_payment_under_review_at_a_time(self) -> None:
        self.start_startups()
        self.assertEqual(self.upgrade().status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.upgrade().status_code, status.HTTP_409_CONFLICT)

    def test_pending_first_payment_swaps_the_plan(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})  # chosen, not paid yet
        response = self.upgrade()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        current = Subscription.objects.order_by("-created_at", "-id").first()
        self.assertEqual((current.plan.code, current.status), ("businesses", "pending_payment"))
        self.assertIsNone(ManualPayment.objects.get().upgrade_plan)  # an ordinary first payment
        verify_payment(ManualPayment.objects.get().pk, self.staff)
        current.refresh_from_db()
        self.assertEqual(current.status, "active")

    def test_lapsed_plan_upgrade_starts_fresh(self) -> None:
        sub = self.start_startups()
        self.upgrade()
        Subscription.objects.filter(pk=sub.pk).update(status="expired", ends_at=timezone.now() - timedelta(days=1))
        verify_payment(ManualPayment.objects.get(upgrade_plan__isnull=False).pk, self.staff)
        sub.refresh_from_db()
        self.assertEqual((sub.plan.code, sub.status), ("businesses", "active"))
        self.assertLess(abs((sub.ends_at - add_months(sub.starts_at, 1)).total_seconds()), 5)
