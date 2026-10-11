"""Company, plans, subscription, manual payments and admin review (portal-v1 §3.5-3.7)."""
from __future__ import annotations

import shutil
import tempfile
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from pathlib import Path

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.defaultfilters import date as date_filter
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import status

from z_analytics.models import Event

from ._testing import COMPANY, PortalTestCase, last_code
from .billing import add_months
from .models import (
    Company,
    EarlyAccessRegistration,
    ManualPayment,
    Plan,
    PlanActivation,
    Subscription,
    User,
)

COMPANY_URL = "/api/auth/company/"
PLANS_URL = "/api/auth/plans/"
SUB_URL = "/api/auth/subscription/"
PAY_URL = "/api/auth/subscription/payment/"
ME = "/api/auth/me/"
ADMIN_PAYMENTS = "/admin/z_auth/manualpayment/"

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n%fake\n"
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64

TEAM = "team@zinwe.test"


def enable_trial(code: str = "businesses", days: int = 90) -> None:
    """No plan ships a trial since 0007, but the trial code stays (an admin can set
    trial_days again). Tests of that code switch a trial on first."""
    Plan.objects.filter(code=code).update(trial_days=days)


def payment_form(**overrides):  # type: ignore[no-untyped-def]
    data = {
        "method": "upi",
        "amount_inr": "999",
        "reference": "UTR123456",
        "paid_on": timezone.localdate().isoformat(),
        "payer_name": "Asha Verma",
    }
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


class MediaMixin:
    """Uploads go to a throwaway MEDIA_ROOT."""

    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        self.media = tempfile.mkdtemp(prefix="zinwe-media-")
        override = override_settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(shutil.rmtree, self.media, True)


# --- Company ----------------------------------------------------------------------


class CompanyTests(PortalTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = self.make_owner()

    def test_get_before_registering(self) -> None:
        response = self.get(COMPANY_URL)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.json(), {"detail": "No company yet."})

    def test_create(self) -> None:
        response = self.post(COMPANY_URL, COMPANY)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        body = response.json()
        company = Company.objects.get()
        self.assertEqual(
            body,
            {
                "id": company.id, **COMPANY, "country": "India", "gstin": "", "website": "",
                "created_at": body["created_at"],
            },
        )
        self.assertEqual(company.owner, self.user)
        self.user.refresh_from_db()
        self.assertEqual(self.user.company, "Verma Textiles")  # legacy field kept in sync
        self.assertEqual(self.get(COMPANY_URL).json(), body)
        self.assertTrue(Event.objects.filter(name="company.registered").exists())

    def test_one_company_per_owner(self) -> None:
        self.post(COMPANY_URL, COMPANY)
        response = self.post(COMPANY_URL, {**COMPANY, "name": "Second"})
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.json(), {"detail": "Your company is already registered."})
        self.assertEqual(Company.objects.count(), 1)

    def test_required_fields(self) -> None:
        response = self.post(COMPANY_URL, {})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        body = response.json()
        for field in ("name", "phone", "city", "state"):
            self.assertEqual(body[field], ["This field is required."], field)
        self.assertEqual(body["industry"], ["Pick one option."])
        self.assertEqual(body["team_size"], ["Pick one option."])
        self.assertFalse(Company.objects.exists())

    def test_bad_choices_and_phone(self) -> None:
        response = self.post(COMPANY_URL, {**COMPANY, "industry": "mining", "team_size": "1000", "phone": "12"})
        body = response.json()
        self.assertEqual(body["industry"], ["Pick one option."])
        self.assertEqual(body["team_size"], ["Pick one option."])
        self.assertEqual(body["phone"], ["Enter a valid phone number."])

    def test_gstin_rules(self) -> None:
        bad = self.post(COMPANY_URL, {**COMPANY, "gstin": "07ABCDE1234F1X5"})  # 14th char must be Z
        self.assertEqual(bad.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(bad.json(), {"gstin": ["Enter a valid 15-character GSTIN, e.g. 07ABCDE1234F1Z5."]})
        short = self.post(COMPANY_URL, {**COMPANY, "gstin": "07ABCDE1234"})
        self.assertIn("gstin", short.json())
        ok = self.post(COMPANY_URL, {**COMPANY, "gstin": " 07abcde1234f1z5 "})
        self.assertEqual(ok.status_code, status.HTTP_201_CREATED, ok.content)
        self.assertEqual(ok.json()["gstin"], "07ABCDE1234F1Z5")

    def test_website_rules(self) -> None:
        bad = self.post(COMPANY_URL, {**COMPANY, "website": "not a site"})
        self.assertEqual(bad.json(), {"website": ["Enter a valid website address, e.g. https://example.com."]})
        ok = self.post(COMPANY_URL, {**COMPANY, "website": "vermatextiles.in", "country": "  "})
        self.assertEqual(ok.status_code, status.HTTP_201_CREATED)
        self.assertEqual(ok.json()["website"], "https://vermatextiles.in")
        self.assertEqual(ok.json()["country"], "India")

    def test_patch(self) -> None:
        self.post(COMPANY_URL, COMPANY)
        response = self.patch(COMPANY_URL, {"name": "Verma Fabrics", "gstin": "07ABCDE1234F1Z5"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["name"], "Verma Fabrics")
        self.assertEqual(response.json()["city"], "New Delhi")  # untouched
        self.user.refresh_from_db()
        self.assertEqual(self.user.company, "Verma Fabrics")

        bad = self.patch(COMPANY_URL, {"name": "", "gstin": "nope"})
        self.assertEqual(bad.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(set(bad.json()), {"name", "gstin"})

    def test_patch_without_company(self) -> None:
        response = self.patch(COMPANY_URL, {"name": "X"})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.json(), {"detail": "No company yet."})

    def test_requires_auth(self) -> None:
        self.use_token(None)
        for method in (self.get, self.post, self.patch):
            self.assertEqual(method(COMPANY_URL).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_owners_only_see_their_own_company(self) -> None:
        other = User.objects.create_user("other@acme.in", "x-long-password-1")
        self.make_company(other, name="Other Co")
        self.assertEqual(self.get(COMPANY_URL).status_code, status.HTTP_404_NOT_FOUND)


# --- Plans --------------------------------------------------------------------------


class PlanListTests(PortalTestCase):
    def test_plan_shape(self) -> None:
        response = self.get(PLANS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        plans = {p["code"]: p for p in response.json()}
        self.assertEqual(list(plans), ["free", "startups", "businesses"])
        self.assertEqual(
            plans["businesses"],
            {
                "code": "businesses", "name": "Businesses", "tagline": plans["businesses"]["tagline"],
                "price_inr_monthly": "2999.00", "trial_days": 0, "max_devices": 50,
                "features": plans["businesses"]["features"], "is_recommended": True,
                "requires_payment": True, "sort_order": 30, "trial_available": False,
            },
        )
        self.assertTrue(plans["businesses"]["tagline"])
        # Free forever is the free way in: no plan offers a time-limited trial.
        self.assertEqual([p["trial_days"] for p in plans.values()], [0, 0, 0])
        self.assertNotIn("First 3 months free", plans["businesses"]["features"])
        self.assertEqual(len(plans["businesses"]["features"]), 4)
        self.assertEqual(plans["free"]["name"], "Free forever")
        self.assertEqual(plans["free"]["max_devices"], 3)
        self.assertFalse(plans["free"]["requires_payment"])
        self.assertFalse(plans["free"]["trial_available"])
        self.assertEqual(
            (plans["startups"]["price_inr_monthly"], plans["startups"]["max_devices"]), ("999.00", 10)
        )
        self.assertTrue(plans["startups"]["requires_payment"])
        self.assertFalse(plans["startups"]["trial_available"])  # no trial on Startups
        self.assertFalse(plans["startups"]["is_recommended"])

    def trial_available(self) -> bool:
        plans = {p["code"]: p for p in self.get(PLANS_URL).json()}
        return plans["businesses"]["trial_available"]

    def test_trial_available_anonymous(self) -> None:
        enable_trial()
        self.assertTrue(self.trial_available())

    def test_trial_available_until_used(self) -> None:
        enable_trial()
        user = self.make_owner()  # client now sends the token
        self.make_company(user)
        self.assertTrue(self.trial_available())
        self.post(SUB_URL, {"plan": "businesses"})
        self.assertFalse(self.trial_available())
        # Nested plans in the owner's own subscription agree.
        self.assertFalse(self.get(SUB_URL).json()["subscription"]["plan"]["trial_available"])
        self.assertFalse(self.get(ME).json()["subscription"]["plan"]["trial_available"])
        # Still public: other visitors see the trial.
        self.use_token(None)
        self.assertTrue(self.trial_available())

    def test_legacy_early_access_counts_as_trial_used(self) -> None:
        enable_trial()
        user = self.make_owner()
        PlanActivation.objects.create(user=user, plan=Plan.objects.get(code=Plan.EARLY_ACCESS), is_active=False)
        self.assertFalse(self.trial_available())

    def test_stale_token_is_treated_as_anonymous(self) -> None:
        enable_trial()
        self.use_token("0" * 40)
        response = self.get(PLANS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue({p["code"]: p for p in response.json()}["businesses"]["trial_available"])


# --- Subscription -----------------------------------------------------------------


@override_settings(ZINWE_TEAM_NOTIFY_EMAIL=TEAM)
class SubscriptionTests(PortalTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = self.make_owner()
        self.company = self.make_company(self.user)

    def subscribe(self, plan: str):  # type: ignore[no-untyped-def]
        return self.post(SUB_URL, {"plan": plan})

    def step(self) -> str:
        return self.get(ME).json()["onboarding"]["step"]

    def test_get_without_subscription(self) -> None:
        response = self.get(SUB_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertIsNone(body["subscription"])
        self.assertEqual(
            set(body["instructions"]),
            {"upi_id", "payee_name", "bank_name", "account_type", "account_number", "ifsc", "paypal_url", "note"},
        )

    @override_settings(
        ZINWE_PAYMENT_INSTRUCTIONS={
            "upi_id": "acme@okbank", "payee_name": "SKYTECHNO", "bank_name": "Test Bank",
            "account_type": "Current", "account_number": "123456789012", "ifsc": "TEST0001234",
            "paypal_url": "https://paypal.me/zinwetest", "note": "Use your company name.",
        }
    )
    def test_instructions_include_account_type_and_paypal(self) -> None:
        instructions = self.get(SUB_URL).json()["instructions"]
        self.assertEqual(instructions["account_type"], "Current")
        self.assertEqual(instructions["paypal_url"], "https://paypal.me/zinwetest")
        self.assertEqual(instructions["account_number"], "123456789012")

    def test_company_required(self) -> None:
        self.company.delete()
        response = self.subscribe("free")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json(), {"detail": "Register your company first."})
        self.assertFalse(Subscription.objects.exists())

    def test_free_plan_is_active_at_once(self) -> None:
        response = self.subscribe("free")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        body = response.json()
        self.assertEqual(body["status"], "active")
        self.assertEqual(body["plan"]["code"], "free")
        self.assertEqual(body["amount_inr"], "0.00")
        self.assertEqual(body["billing_cycle"], "monthly")
        self.assertIsNotNone(body["starts_at"])
        self.assertIsNone(body["ends_at"])
        self.assertIsNone(body["latest_payment"])
        self.assertFalse(body["is_trial"])
        activation = PlanActivation.objects.get(user=self.user, is_active=True)
        self.assertEqual(activation.plan.code, "free")
        self.assertEqual(Subscription.objects.get().activation, activation)
        self.assertEqual([m.subject for m in mail.outbox], ["Your Zinwe Free forever plan is active"])
        self.assertEqual(self.step(), "complete")
        self.assertEqual(self.get(SUB_URL).json()["subscription"]["id"], body["id"])

    def test_businesses_waits_for_payment(self) -> None:
        body = self.subscribe("businesses").json()
        self.assertEqual((body["status"], body["is_trial"], body["amount_inr"]), ("pending_payment", False, "2999.00"))
        self.assertFalse(PlanActivation.objects.exists())
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(self.step(), "payment")

    def test_plan_with_trial_starts_as_free_trial(self) -> None:
        enable_trial()
        reg = EarlyAccessRegistration.objects.create(
            name="Asha", email="owner@acme.in", company="Acme", team_size="1-10"
        )
        response = self.subscribe("businesses")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        body = response.json()
        self.assertEqual(body["status"], "active")
        self.assertTrue(body["is_trial"])
        self.assertEqual(body["amount_inr"], "0.00")
        self.assertEqual(body["plan"]["code"], "businesses")
        self.assertIsNone(body["latest_payment"])
        ends_at = datetime.fromisoformat(body["ends_at"])
        self.assertLess(abs((ends_at - (timezone.now() + timedelta(days=90))).total_seconds()), 60)

        subscription = Subscription.objects.get()
        activation = PlanActivation.objects.get(user=self.user, is_active=True)
        self.assertEqual((activation.plan.code, activation.source), ("businesses", "trial"))
        self.assertEqual(subscription.activation, activation)
        self.assertEqual(activation.ends_at, subscription.ends_at)
        reg.refresh_from_db()
        self.assertEqual(reg.status, "activated")

        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertEqual(email.subject, "Your free Zinwe Businesses trial has started")
        self.assertIn("Your free 3-month trial of Businesses has started", email.body)
        self.assertIn(f"It ends on {date_filter(timezone.localtime(subscription.ends_at), 'j F Y')}.", email.body)
        self.assertNotIn("Paid until", email.body)
        self.assertEqual(self.step(), "complete")
        self.assertTrue(Event.objects.filter(name="subscription.created", properties__is_trial=True).exists())

    def test_trial_is_once_per_account(self) -> None:
        enable_trial()
        self.subscribe("businesses")
        Subscription.objects.update(ends_at=timezone.now() - timedelta(seconds=1))  # trial over
        self.assertEqual(self.get(SUB_URL).json()["subscription"]["status"], "expired")
        self.assertFalse(PlanActivation.objects.filter(is_active=True).exists())
        self.assertEqual(self.step(), "plan")

        mail.outbox.clear()
        response = self.subscribe("businesses")  # now it means paying
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()
        self.assertEqual(body["status"], "pending_payment")
        self.assertFalse(body["is_trial"])
        self.assertEqual(body["amount_inr"], "2999.00")
        self.assertIsNone(body["starts_at"])
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(self.step(), "payment")
        self.assertEqual(Subscription.objects.filter(is_trial=True).count(), 1)

    def test_legacy_early_access_activation_counts_as_trial_used(self) -> None:
        enable_trial()
        PlanActivation.objects.create(
            user=self.user, plan=Plan.objects.get(code=Plan.EARLY_ACCESS), is_active=False
        )
        body = self.subscribe("businesses").json()
        self.assertEqual(body["status"], "pending_payment")
        self.assertFalse(body["is_trial"])
        self.assertEqual(body["amount_inr"], "2999.00")

    def test_legacy_free_trial_plan_can_be_used_once(self) -> None:
        # A free plan with trial_days (the old early-access shape) is a trial too.
        enable_trial()
        enable_trial(Plan.EARLY_ACCESS)
        Plan.objects.filter(code=Plan.EARLY_ACCESS).update(is_active=True)
        body = self.subscribe("early-access").json()
        self.assertEqual((body["status"], body["is_trial"]), ("active", True))
        self.assertIn("Your free 3-month trial of Early access has started", mail.outbox[0].body)
        Subscription.objects.update(ends_at=timezone.now() - timedelta(seconds=1))
        self.get(SUB_URL)  # expires it

        response = self.subscribe("early-access")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json(), {"plan": ["You've already used this trial."]})
        # ...and the trial on Businesses is gone too: one trial per account.
        self.assertEqual(self.subscribe("businesses").json()["status"], "pending_payment")

    def test_startups_waits_for_payment(self) -> None:
        response = self.subscribe("startups")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()
        self.assertEqual(body["status"], "pending_payment")
        self.assertFalse(body["is_trial"])
        self.assertEqual(body["amount_inr"], "999.00")
        self.assertIsNone(body["starts_at"])
        self.assertFalse(PlanActivation.objects.exists())
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(self.step(), "payment")

    def test_pending_subscription_is_replaced(self) -> None:
        enable_trial()
        first = self.subscribe("startups").json()
        second = self.subscribe("startups").json()
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(Subscription.objects.get(pk=first["id"]).status, "cancelled")
        third = self.subscribe("businesses").json()  # pending -> trial is fine
        self.assertEqual((third["status"], third["is_trial"]), ("active", True))
        self.assertEqual(Subscription.objects.get(pk=second["id"]).status, "cancelled")

    def test_active_subscription_conflicts(self) -> None:
        self.subscribe("free")
        for code in ("startups", "businesses"):
            response = self.subscribe(code)
            self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
            self.assertIn("detail", response.json())

    def test_active_trial_conflicts(self) -> None:
        enable_trial()
        self.subscribe("businesses")
        self.assertEqual(self.subscribe("free").status_code, status.HTTP_409_CONFLICT)

    def test_unknown_or_inactive_plan(self) -> None:
        Plan.objects.create(code="legacy", name="Legacy", is_active=False)
        for code in ("legacy", "pro", "early-access", "nope"):
            response = self.subscribe(code)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(response.json(), {"plan": ["That plan doesn't exist or isn't available."]})
        self.assertEqual(self.post(SUB_URL, {}).json(), {"plan": ["This field is required."]})

    def test_requires_auth(self) -> None:
        self.use_token(None)
        self.assertEqual(self.get(SUB_URL).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.subscribe("free").status_code, status.HTTP_401_UNAUTHORIZED)


# --- Manual payments --------------------------------------------------------------


@override_settings(ZINWE_TEAM_NOTIFY_EMAIL=TEAM)
class PaymentTests(MediaMixin, PortalTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = self.make_owner()
        self.make_company(self.user)
        self.subscription_id = self.post(SUB_URL, {"plan": "startups"}).json()["id"]

    def pay(self, proof: SimpleUploadedFile | None = None, **overrides):  # type: ignore[no-untyped-def]
        data = payment_form(**overrides)
        if proof is not None:
            data["proof"] = proof
        return self.post(PAY_URL, data, fmt="multipart")

    def test_submit_with_proof(self) -> None:
        response = self.pay(SimpleUploadedFile("receipt.PNG", PNG, content_type="image/png"))

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        body = response.json()
        payment = ManualPayment.objects.get()
        self.assertEqual(
            body,
            {
                "id": payment.id, "method": "upi", "amount_inr": "999.00", "reference": "UTR123456",
                "paid_on": timezone.localdate().isoformat(), "payer_name": "Asha Verma",
                "status": "submitted", "rejection_reason": "", "has_proof": True,
                "submitted_at": body["submitted_at"], "reviewed_at": None,
                "is_renewal": False, "covers_from": None, "covers_until": None, "plan_name": "Startups",
                "upgrade_plan_name": None,
            },
        )
        now = timezone.now()
        self.assertRegex(payment.proof.name, rf"^payments/{now:%Y}/{now:%m}/[0-9a-f]{{32}}\.png$")
        self.assertTrue((Path(self.media) / payment.proof.name).exists())

        self.assertEqual(len(mail.outbox), 1)
        alert = mail.outbox[0]
        self.assertEqual(alert.to, [TEAM])
        self.assertIn("Verma Textiles", alert.subject)
        self.assertIn("UTR123456", alert.body)
        self.assertTrue(Event.objects.filter(name="payment.submitted").exists())

        me = self.get(ME).json()
        self.assertEqual(me["onboarding"], {"step": "payment_review", "has_company": True, "subscription_status": "pending_payment"})
        self.assertEqual(me["subscription"]["latest_payment"]["id"], payment.id)

    def test_submit_without_proof_and_other_types(self) -> None:
        self.assertEqual(self.pay().json()["has_proof"], False)
        for name, content in (("r.pdf", PDF), ("r.jpg", JPG), ("r.jpeg", JPG)):
            ManualPayment.objects.update(status="rejected")
            with self.subTest(name=name):
                response = self.pay(SimpleUploadedFile(name, content))
                self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)

    def test_rejects_wrong_file_types(self) -> None:
        cases = [
            ("r.gif", b"GIF89a" + b"\x00" * 10),
            ("r.txt", b"hello"),
            ("r.png", b"not really a png"),  # extension lies
            ("noext", PNG),
        ]
        for name, content in cases:
            with self.subTest(name=name):
                response = self.pay(SimpleUploadedFile(name, content))
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(response.json(), {"proof": ["Upload a JPG, PNG or PDF file."]})
        self.assertFalse(ManualPayment.objects.exists())

    def test_rejects_oversize_file(self) -> None:
        big = SimpleUploadedFile("big.pdf", b"%PDF-" + b"0" * (5 * 1024 * 1024))
        response = self.pay(big)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json(), {"proof": ["The file is too large. The limit is 5 MB."]})

    def test_field_validation(self) -> None:
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        response = self.pay(method="crypto", amount_inr="0", paid_on=tomorrow, reference="", payer_name=None)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        body = response.json()
        self.assertEqual(body["method"], ["Pick one option."])
        self.assertEqual(body["amount_inr"], ["Enter the amount you paid, e.g. 1999."])
        self.assertEqual(body["paid_on"], ["The payment date can't be in the future."])
        self.assertEqual(body["reference"], ["This field is required."])
        self.assertEqual(body["payer_name"], ["This field is required."])
        self.assertEqual(self.pay(paid_on="09/10/2026").json()["paid_on"], ["Enter the date as YYYY-MM-DD."])

    def test_paypal_is_accepted(self) -> None:
        response = self.pay(method="paypal", reference="8AB12345CD678901E")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        self.assertEqual(response.json()["method"], "paypal")
        self.assertEqual(ManualPayment.objects.get().get_method_display(), "PayPal")
        alert = mail.outbox[0]
        self.assertIn("(PayPal)", alert.subject)
        self.assertIn("999.00 INR by PayPal", alert.body)
        self.assertIn("by PayPal", alert.alternatives[0][0])

    def test_one_payment_under_review_at_a_time(self) -> None:
        self.pay()
        response = self.pay()
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.json(), {"detail": "Your payment is being reviewed. You can change plans once we've checked it."})
        self.assertEqual(ManualPayment.objects.count(), 1)

    def test_plan_change_blocked_while_payment_under_review(self) -> None:
        self.pay()
        response = self.post(SUB_URL, {"plan": "free"})
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_nothing_to_pay(self) -> None:
        Subscription.objects.update(status="cancelled")
        response = self.pay()
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.json(), {"detail": "There's no payment due on your subscription."})

    def test_nothing_to_pay_on_free_plan(self) -> None:
        other = self.make_owner("free@acme.in")
        self.make_company(other)
        self.post(SUB_URL, {"plan": "free"})
        self.assertEqual(self.pay().status_code, status.HTTP_409_CONFLICT)

    def test_requires_auth(self) -> None:
        self.use_token(None)
        self.assertEqual(self.pay().status_code, status.HTTP_401_UNAUTHORIZED)

    def test_throttled_after_10_per_hour(self) -> None:
        for _ in range(10):
            self.pay(method="bad")
        self.assertEqual(self.pay().status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_owner_can_download_own_proof(self) -> None:
        payment_id = self.pay(SimpleUploadedFile("r.pdf", PDF)).json()["id"]
        url = f"{PAY_URL}{payment_id}/proof/"
        response = self.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(b"".join(response.streaming_content), PDF)
        self.assertIn("attachment", response["Content-Disposition"])

        self.make_owner("stranger@acme.in")  # switches the client's token
        self.assertEqual(self.get(url).status_code, status.HTTP_404_NOT_FOUND)

    def test_proof_download_404_without_file(self) -> None:
        payment_id = self.pay().json()["id"]
        self.assertEqual(self.get(f"{PAY_URL}{payment_id}/proof/").status_code, status.HTTP_404_NOT_FOUND)

    def test_media_is_not_served_publicly(self) -> None:
        payment_id = self.pay(SimpleUploadedFile("r.pdf", PDF)).json()["id"]
        name = ManualPayment.objects.get(pk=payment_id).proof.name
        self.use_token(None)
        self.assertEqual(self.client.get(f"/media/{name}").status_code, status.HTTP_404_NOT_FOUND)


# --- Admin review -------------------------------------------------------------------


@override_settings(ZINWE_TEAM_NOTIFY_EMAIL=TEAM)
class AdminReviewTests(MediaMixin, PortalTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.owner = self.make_owner()
        self.make_company(self.owner)
        self.post(SUB_URL, {"plan": "startups"})
        self.payment_id = self.post(
            PAY_URL, {**payment_form(), "proof": SimpleUploadedFile("r.png", PNG)}, fmt="multipart"
        ).json()["id"]
        self.owner_token = self.client._credentials["HTTP_AUTHORIZATION"].split()[1]
        self.admin = User.objects.create_superuser("admin@zinwe.local", "x-long-password-1")
        self.client.force_login(self.admin)
        mail.outbox.clear()

    def run_action(self, action: str, **extra):  # type: ignore[no-untyped-def]
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                ADMIN_PAYMENTS, {"action": action, "_selected_action": [self.payment_id], **extra}
            )

    def owner_view(self, url: str):  # type: ignore[no-untyped-def]
        self.client.logout()
        self.use_token(self.owner_token)
        try:
            return self.get(url).json()
        finally:
            self.use_token(None)
            self.client.force_login(self.admin)

    def test_verify_activates_for_a_month(self) -> None:
        response = self.run_action("verify_payments")
        self.assertEqual(response.status_code, 302)

        payment = ManualPayment.objects.get()
        self.assertEqual(payment.status, "verified")
        self.assertEqual(payment.reviewed_by, self.admin)
        self.assertIsNotNone(payment.reviewed_at)
        sub = Subscription.objects.get(status="active")
        self.assertEqual(sub.ends_at, add_months(sub.starts_at, 1))
        self.assertLess(abs((sub.starts_at - timezone.now()).total_seconds()), 60)
        activation = PlanActivation.objects.get(user=self.owner, is_active=True)
        self.assertEqual((activation.plan.code, activation.source), ("startups", "manual-payment"))
        self.assertEqual(activation.ends_at, sub.ends_at)
        self.assertEqual(sub.activation, activation)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["owner@acme.in"])
        self.assertEqual(mail.outbox[0].subject, "Your Zinwe Startups plan is active")
        self.assertIn("Paid until", mail.outbox[0].body)
        self.assertTrue(Event.objects.filter(name="payment.verified").exists())

        me = self.owner_view(ME)
        self.assertEqual(me["onboarding"], {"step": "complete", "has_company": True, "subscription_status": "active"})
        self.assertEqual(me["active_plan"]["plan"]["code"], "startups")
        self.assertFalse(me["subscription"]["is_trial"])
        self.assertEqual(me["subscription"]["latest_payment"]["status"], "verified")

    def test_verify_businesses_after_trial(self) -> None:
        """The one-trial rule must not block paying for the plan the trial was on."""
        enable_trial()
        self.client.logout()  # drop the admin session (also clears credentials)
        other = self.make_owner("trial@acme.in")  # client now uses this owner's token
        self.make_company(other)
        self.post(SUB_URL, {"plan": "businesses"})  # free trial
        Subscription.objects.filter(owner=other).update(ends_at=timezone.now() - timedelta(seconds=1))
        again = self.post(SUB_URL, {"plan": "businesses"})
        self.assertEqual(again.json().get("status"), "pending_payment", again.content)
        payment_id = self.post(
            PAY_URL, payment_form(amount_inr="2999", method="bank_transfer"), fmt="multipart"
        ).json()["id"]
        self.use_token(None)
        self.client.force_login(self.admin)
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(ADMIN_PAYMENTS, {"action": "verify_payments", "_selected_action": [payment_id]})

        self.assertEqual(ManualPayment.objects.get(pk=payment_id).status, "verified")
        paid = Subscription.objects.get(owner=other, status="active")
        self.assertEqual((paid.plan.code, paid.is_trial, str(paid.amount_inr)), ("businesses", False, "2999.00"))
        self.assertEqual(paid.ends_at, add_months(paid.starts_at, 1))
        activation = PlanActivation.objects.get(user=other, is_active=True)
        self.assertEqual((activation.plan.code, activation.source), ("businesses", "manual-payment"))
        self.assertEqual(PlanActivation.objects.filter(user=other, plan__code="businesses").count(), 2)
        self.assertEqual(mail.outbox[0].subject, "Your Zinwe Businesses plan is active")
        self.assertIn("Paid until", mail.outbox[0].body)

    def test_verify_keeps_one_active_plan(self) -> None:
        PlanActivation.objects.create(user=self.owner, plan=Plan.objects.get(code="free"))
        self.run_action("verify_payments")
        self.assertEqual(PlanActivation.objects.filter(user=self.owner, is_active=True).count(), 1)

    def test_verify_twice_is_refused(self) -> None:
        self.run_action("verify_payments")
        mail.outbox.clear()
        response = self.run_action("verify_payments")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(PlanActivation.objects.count(), 1)

    def test_reject_asks_for_a_reason_first(self) -> None:
        response = self.run_action("reject_payments")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reject payment")
        self.assertContains(response, 'name="reason"')
        self.assertEqual(ManualPayment.objects.get().status, "submitted")

        response = self.run_action("reject_payments", apply="1", reason="   ")
        self.assertEqual(response.status_code, 200)  # form shown again with an error
        self.assertEqual(ManualPayment.objects.get().status, "submitted")
        self.assertEqual(len(mail.outbox), 0)

    def test_reject_then_resubmit(self) -> None:
        response = self.run_action("reject_payments", apply="1", reason="No payment with this UTR.")
        self.assertEqual(response.status_code, 302)

        payment = ManualPayment.objects.get()
        self.assertEqual((payment.status, payment.rejection_reason), ("rejected", "No payment with this UTR."))
        self.assertEqual(Subscription.objects.get().status, "pending_payment")
        self.assertFalse(PlanActivation.objects.exists())
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "We couldn't confirm your Zinwe payment")
        self.assertIn("No payment with this UTR.", mail.outbox[0].body)
        self.assertTrue(Event.objects.filter(name="payment.rejected").exists())

        me = self.owner_view(ME)
        self.assertEqual(me["onboarding"]["step"], "payment")
        self.assertEqual(me["subscription"]["latest_payment"]["rejection_reason"], "No payment with this UTR.")

        self.client.logout()
        self.use_token(self.owner_token)
        response = self.post(PAY_URL, payment_form(reference="UTR999"), fmt="multipart")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.get(ME).json()["onboarding"]["step"], "payment_review")

    def test_admin_pages_and_proof_download(self) -> None:
        for url in (
            ADMIN_PAYMENTS,
            f"{ADMIN_PAYMENTS}{self.payment_id}/change/",
            "/admin/z_auth/subscription/",
            f"/admin/z_auth/subscription/{Subscription.objects.get().pk}/change/",
            "/admin/z_auth/company/",
            "/admin/z_auth/onetimecode/",
            "/admin/z_auth/plan/",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
        change = self.client.get(f"{ADMIN_PAYMENTS}{self.payment_id}/change/")
        self.assertContains(change, f"{ADMIN_PAYMENTS}{self.payment_id}/proof/")
        proof = self.client.get(f"{ADMIN_PAYMENTS}{self.payment_id}/proof/")
        self.assertEqual(proof.status_code, 200)
        self.assertEqual(b"".join(proof.streaming_content), PNG)

    def test_proof_download_needs_staff(self) -> None:
        self.client.logout()
        response = self.client.get(f"{ADMIN_PAYMENTS}{self.payment_id}/proof/")
        self.assertEqual(response.status_code, 302)  # to the admin login


# --- Whole journeys -----------------------------------------------------------------


@override_settings(ZINWE_TEAM_NOTIFY_EMAIL=TEAM)
class JourneyTests(MediaMixin, PortalTestCase):
    def step(self) -> dict:
        return self.get(ME).json()["onboarding"]

    def test_signup_to_free_plan(self) -> None:
        body = self.signup_and_verify()
        self.assertEqual(body["onboarding"]["step"], "company")
        self.assertEqual(self.post(COMPANY_URL, COMPANY).status_code, 201)
        self.assertEqual(self.step(), {"step": "plan", "has_company": True, "subscription_status": None})
        self.assertEqual(self.post(SUB_URL, {"plan": "free"}).status_code, 201)
        self.assertEqual(self.step(), {"step": "complete", "has_company": True, "subscription_status": "active"})
        # Signing in again lands on the same step.
        self.use_token(None)
        response = self.post("/api/auth/login/", {"email": "asha@acme.in", "password": "s3cure-Passphrase!"})
        self.assertEqual(response.json()["onboarding"]["step"], "complete")

    def test_signup_to_paid_plan_with_admin_verification(self) -> None:
        self.signup_and_verify()
        self.post(COMPANY_URL, COMPANY)
        self.post(SUB_URL, {"plan": "startups"})
        self.assertEqual(self.step()["step"], "payment")
        response = self.post(
            PAY_URL, {**payment_form(), "proof": SimpleUploadedFile("r.jpg", JPG)}, fmt="multipart"
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.step()["step"], "payment_review")

        token = self.client._credentials["HTTP_AUTHORIZATION"]
        admin = User.objects.create_superuser("admin@zinwe.local", "x-long-password-1")
        self.use_token(None)
        self.client.force_login(admin)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                ADMIN_PAYMENTS,
                {"action": "verify_payments", "_selected_action": [response.json()["id"]]},
            )
        self.client.logout()
        self.client.credentials(HTTP_AUTHORIZATION=token)
        self.assertEqual(self.step(), {"step": "complete", "has_company": True, "subscription_status": "active"})
        self.assertIn("Your Zinwe Startups plan is active", [m.subject for m in mail.outbox])

    def test_signup_to_businesses_needs_payment(self) -> None:
        self.signup_and_verify()
        self.post(COMPANY_URL, COMPANY)
        plans = {p["code"]: p for p in self.get(PLANS_URL).json()}
        self.assertFalse(plans["businesses"]["trial_available"])
        body = self.post(SUB_URL, {"plan": "businesses"}).json()
        self.assertEqual((body["status"], body["is_trial"], body["amount_inr"]), ("pending_payment", False, "2999.00"))
        self.assertEqual(self.step()["step"], "payment")
        self.assertNotIn("Your free Zinwe Businesses trial has started", [m.subject for m in mail.outbox])

    def test_otp_login_and_password_reset_route_by_step(self) -> None:
        self.signup_and_verify()
        self.post(COMPANY_URL, COMPANY)
        self.use_token(None)
        self.post("/api/auth/login/otp/request/", {"email": "asha@acme.in"})
        body = self.post(
            "/api/auth/login/otp/verify/", {"email": "asha@acme.in", "code": last_code("asha@acme.in")}
        ).json()
        self.assertEqual(body["onboarding"]["step"], "plan")
        self.post("/api/auth/password/forgot/", {"email": "asha@acme.in"})
        body = self.post(
            "/api/auth/password/reset/",
            {"email": "asha@acme.in", "code": last_code("asha@acme.in"), "new_password": "Br4nd-new-Secret!"},
        ).json()
        self.assertEqual(body["onboarding"]["step"], "plan")


class HelperTests(TestCase):
    def test_add_months_clamps_to_month_end(self) -> None:
        utc = dt_timezone.utc
        self.assertEqual(add_months(datetime(2026, 1, 31, tzinfo=utc)), datetime(2026, 2, 28, tzinfo=utc))
        self.assertEqual(add_months(datetime(2028, 1, 31, tzinfo=utc)), datetime(2028, 2, 29, tzinfo=utc))
        self.assertEqual(add_months(datetime(2026, 12, 15, tzinfo=utc)), datetime(2027, 1, 15, tzinfo=utc))

    def test_plan_requires_payment(self) -> None:
        self.assertTrue(Plan(price_inr_monthly=Decimal("1")).requires_payment)
        self.assertFalse(Plan(price_inr_monthly=Decimal("0")).requires_payment)


ANNOUNCE_URL = "/api/auth/announcements/"


class AnnouncementsTests(PortalTestCase):
    """The dashboard notification-bar endpoint (M11): config banners + the upgrade nudge."""

    def test_requires_auth(self) -> None:
        self.assertEqual(self.get(ANNOUNCE_URL).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_upgrade_nudge_when_below_top_plan(self) -> None:
        user = self.make_owner()
        self.make_company(user)
        items = self.get(ANNOUNCE_URL).json()["announcements"]
        upgrade = [a for a in items if a["kind"] == "upgrade"]
        self.assertEqual(len(upgrade), 1)
        self.assertEqual(upgrade[0]["cta_href"], "/app/settings/billing")
        self.assertTrue(upgrade[0]["id"].startswith("upgrade:none:"))
        self.assertTrue(upgrade[0]["dismissible"])

    def test_no_upgrade_on_top_plan(self) -> None:
        user = self.make_owner()
        self.make_company(user)
        enable_trial()
        self.post(SUB_URL, {"plan": "businesses"})  # free trial -> active on the top plan
        items = self.get(ANNOUNCE_URL).json()["announcements"]
        self.assertEqual([a for a in items if a["kind"] == "upgrade"], [])

    @override_settings(
        ZINWE_ANNOUNCEMENTS=[
            {
                "id": "v-0-2", "kind": "version", "title": "Zinwe 0.2 is out",
                "cta_label": "What's new", "cta_href": "https://zinwe.io/changelog",
            },
            {"kind": "offer", "title": "no id -> dropped"},
            {"id": "bad-kind", "kind": "banner", "title": "bad kind -> dropped"},
        ]
    )
    def test_config_banners_are_validated(self) -> None:
        user = self.make_owner()
        self.make_company(user)
        items = self.get(ANNOUNCE_URL).json()["announcements"]
        ids = [a["id"] for a in items]
        self.assertIn("v-0-2", ids)
        self.assertNotIn("bad-kind", ids)
        self.assertEqual(sum(1 for a in items if a["kind"] == "version"), 1)
        # Config banners come before the derived upgrade nudge.
        self.assertEqual(items[0]["id"], "v-0-2")
