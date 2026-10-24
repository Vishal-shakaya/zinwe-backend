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
        "method": "upi", "amount_inr": "2834", "reference": "UTR-UP",
        "paid_on": timezone.localdate().isoformat(), "payer_name": "Asha Verma", "accept_terms": "true",
        **overrides,
    }


class _UpgradeBase(PortalTestCase):
    """Shared setup + helpers (no tests of its own, so subclasses don't re-run them)."""

    def setUp(self) -> None:
        super().setUp()
        self.owner = self.make_owner()
        self.make_company(self.owner)
        self.staff = User.objects.create_superuser("ops@zinwe.local", "ops-12345678")

    def start_startups(self) -> Subscription:
        """Startups with its first payment approved: active for a month."""
        self.post(SUB_URL, {"plan": "startups"})
        self.post(PAY_URL, payment_form(amount_inr="971", reference="UTR-1"), fmt="multipart")
        verify_payment(ManualPayment.objects.get().pk, self.staff)
        mail.outbox.clear()
        return Subscription.objects.get()

    def upgrade(self, plan: str = "businesses"):  # type: ignore[no-untyped-def]
        return self.post(PAY_URL, payment_form(upgrade_plan=plan), fmt="multipart")



class UpgradeTests(_UpgradeBase):
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
        self.assertEqual((sub.plan.code, sub.status, str(sub.amount_inr)), ("businesses", "active", "2834.00"))
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


class InvoiceTests(_UpgradeBase):
    def test_invoice_for_a_confirmed_payment(self) -> None:
        self.start_startups()
        payment = ManualPayment.objects.get()
        body = self.get(f"/api/auth/subscription/payments/{payment.pk}/invoice/").json()
        self.assertRegex(body["number"], r"^ZW/\d{4}-\d{2}/0001$")  # gap-free per financial year
        self.assertEqual(body["total_inr"], "971.00")
        self.assertIn("Startups", body["lines"][0]["description"])
        self.assertEqual(body["customer"]["email"], self.owner.email)
        self.assertEqual(body["seller"]["name"], "SKYTECHNO")

    def test_no_invoice_until_confirmed_or_for_others(self) -> None:
        self.post(SUB_URL, {"plan": "startups"})
        self.post(PAY_URL, payment_form(amount_inr="971"), fmt="multipart")
        payment = ManualPayment.objects.get()
        url = f"/api/auth/subscription/payments/{payment.pk}/invoice/"
        self.assertEqual(self.get(url).status_code, status.HTTP_409_CONFLICT)
        verify_payment(payment.pk, self.staff)
        other = self.make_owner("rafi@else.in")
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)


class PaymentTermsTests(_UpgradeBase):
    def test_payment_needs_terms_and_records_them(self) -> None:
        from django.conf import settings

        self.post(SUB_URL, {"plan": "startups"})
        refused = self.post(PAY_URL, payment_form(amount_inr="971", accept_terms="false"), fmt="multipart")
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("accept_terms", refused.json())
        ok = self.post(PAY_URL, payment_form(amount_inr="971"), fmt="multipart")
        self.assertEqual(ok.status_code, status.HTTP_201_CREATED, ok.content)
        payment = ManualPayment.objects.get()
        self.assertEqual(payment.terms_version, settings.ZINWE_LEGAL_VERSION)
        self.assertIsNotNone(payment.terms_accepted_at)


class UpgradeCurrencyTests(PortalTestCase):
    """FIX-004c: "costs more" is judged in the subscriber's own currency and billing cycle, not INR monthly."""

    def setUp(self) -> None:
        super().setUp()
        from decimal import Decimal as D

        from .models import Plan

        self.owner = self.make_owner()
        self.plans = {p.code: p for p in Plan.objects.filter(code__in=("startups", "teams", "businesses"))}
        # INR order is startups < teams < businesses. In USD, Teams is made cheaper than Startups.
        Plan.objects.filter(code="startups").update(price_usd_monthly=D("20"), list_price_usd_monthly=0, price_usd_yearly=D("200"))
        Plan.objects.filter(code="teams").update(price_usd_monthly=D("15"), list_price_usd_monthly=0, price_usd_yearly=D("300"))
        Plan.objects.filter(code="businesses").update(price_usd_monthly=D("40"), list_price_usd_monthly=0, price_usd_yearly=D("400"))
        for code in self.plans:
            self.plans[code].refresh_from_db()

    def sub(self, currency: str, cycle: str) -> Subscription:
        return Subscription.objects.create(
            owner=self.owner, plan=self.plans["startups"], status=Subscription.Status.ACTIVE,
            currency=currency, billing_cycle=cycle, amount_inr=1,
            starts_at=timezone.now(), ends_at=timezone.now() + timedelta(days=20),
        )

    def test_usd_monthly_compares_usd_monthly_prices(self) -> None:
        from .billing import UpgradeNotAllowed, check_upgrade

        sub = self.sub("USD", "monthly")
        with self.assertRaises(UpgradeNotAllowed):  # $15 < $20, though dearer in rupees
            check_upgrade(sub, self.plans["teams"])
        self.assertEqual(check_upgrade(sub, self.plans["businesses"]), sub)

    def test_usd_yearly_compares_usd_yearly_prices(self) -> None:
        from .billing import check_upgrade

        sub = self.sub("USD", "yearly")
        self.assertEqual(check_upgrade(sub, self.plans["teams"]), sub)  # $300/yr > $200/yr

    def test_inr_unchanged(self) -> None:
        from .billing import UpgradeNotAllowed, check_upgrade

        sub = self.sub("INR", "monthly")
        self.assertEqual(check_upgrade(sub, self.plans["teams"]), sub)
        sub.plan = self.plans["businesses"]
        with self.assertRaises(UpgradeNotAllowed):
            check_upgrade(sub, self.plans["teams"])

    def test_payment_target_uses_the_same_rule(self) -> None:
        from .billing import UpgradeNotAllowed, payment_target

        self.sub("USD", "monthly")
        with self.assertRaises(UpgradeNotAllowed):
            payment_target(self.owner, self.plans["teams"])
