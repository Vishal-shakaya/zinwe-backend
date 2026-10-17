from __future__ import annotations

from decimal import Decimal

from datetime import timedelta
from typing import Any

from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from z_analytics.models import Event
from z_notification.models import Notification

from .models import EarlyAccessRegistration, Plan, PlanActivation, User
from z_auth.entitlements import MODULES

EARLY_ACCESS_URL = "/api/auth/early-access/"


def valid_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "Asha Verma",
        "email": "asha@acme.in",
        "company": "Acme Traders",
        "phone": "+91 98100 00000",
        "team_size": "11-25",
        "platforms": ["windows", "linux"],
        "concerns": ["usb", "compliance"],
    }
    payload.update(overrides)
    return payload


# Tests must not depend on the developer's local .env (which may set real addresses).
@override_settings(ZINWE_TEAM_NOTIFY_EMAIL="hello@zinwe.io", DEFAULT_FROM_EMAIL="Zinwe <hello@zinwe.io>")
class EarlyAccessTests(APITestCase):
    def setUp(self) -> None:
        cache.clear()  # reset throttle counters between tests

    def post(self, payload: dict[str, Any], **extra: Any):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(EARLY_ACCESS_URL, payload, format="json", **extra)

    # --- happy path -------------------------------------------------------

    def test_create_returns_201_and_side_effects(self) -> None:
        response = self.post(valid_payload())

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        reg = EarlyAccessRegistration.objects.get()
        self.assertEqual(
            response.json(),
            {
                "id": reg.id,
                "email": "asha@acme.in",
                "status": "pending",
                "trial_days": 0,
                "already_registered": False,
                "message": "Setup call booked.",
            },
        )
        self.assertEqual(reg.plan.code, Plan.EARLY_ACCESS)
        self.assertEqual(reg.trial_days, 0)  # early access starts on Free forever (0007)
        self.assertEqual(reg.ip, "127.0.0.1")

        notifications = Notification.objects.order_by("id")
        self.assertEqual(notifications.count(), 2)
        self.assertEqual(
            [n.template for n in notifications],
            ["early_access_confirmation", "early_access_team_alert"],
        )
        self.assertTrue(all(n.status == Notification.Status.SENT for n in notifications))
        self.assertEqual(notifications[0].recipient, "asha@acme.in")
        self.assertEqual(notifications[1].recipient, "hello@zinwe.io")
        self.assertEqual(notifications[0].ref_id, str(reg.id))

        event = Event.objects.get()
        self.assertEqual(event.name, "early_access.registered")
        self.assertEqual(
            event.properties,
            {"team_size": "11-25", "platforms": ["windows", "linux"], "concerns": ["usb", "compliance"]},
        )

        self.assertEqual(len(mail.outbox), 2)
        confirmation = mail.outbox[0]
        self.assertEqual(confirmation.to, ["asha@acme.in"])
        self.assertIn("Free forever", confirmation.body)
        # J1-03: the "Book a free setup call" story, with a way to start now.
        self.assertIn("free setup call", confirmation.body)
        self.assertIn("Start free now", confirmation.body)
        self.assertIn("/signup", confirmation.body)
        self.assertNotIn("when your workspace is ready", confirmation.body)
        self.assertNotIn("3 months", confirmation.body)
        self.assertIn("Asha", confirmation.body)
        self.assertEqual(len(confirmation.alternatives), 1)  # HTML part
        self.assertIn("Acme Traders", mail.outbox[1].subject)

    def test_normalises_input(self) -> None:
        response = self.post(
            valid_payload(
                name="  Asha Verma  ",
                email="  Asha@ACME.in ",
                company=" Acme ",
                phone="",
                team_size=" 50+ ",
                platforms=["windows", "Windows", " linux", "windows"],
                concerns=[],
            )
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        reg = EarlyAccessRegistration.objects.get()
        self.assertEqual(reg.email, "asha@acme.in")
        self.assertEqual(reg.name, "Asha Verma")
        self.assertEqual(reg.company, "Acme")
        self.assertEqual(reg.team_size, "50+")
        self.assertEqual(reg.platforms, ["windows", "linux"])
        self.assertEqual(reg.concerns, [])

    def test_optional_fields_may_be_omitted(self) -> None:
        payload = valid_payload()
        for key in ("phone", "platforms", "concerns"):
            payload.pop(key)
        response = self.post(payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        reg = EarlyAccessRegistration.objects.get()
        self.assertEqual((reg.phone, reg.platforms, reg.concerns), ("", [], []))

    def test_does_not_require_auth_or_csrf(self) -> None:
        self.client.enforce_csrf_checks = True
        response = self.post(valid_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    # --- duplicates -------------------------------------------------------

    def test_duplicate_returns_200_without_new_row_or_email(self) -> None:
        first = self.post(valid_payload()).json()
        self.assertEqual(len(mail.outbox), 2)

        response = self.post(valid_payload(name="Someone Else"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.json(),
            {
                "id": first["id"],
                "email": "asha@acme.in",
                "status": "pending",
                "trial_days": 0,
                "already_registered": True,
                "message": "You've already booked a setup call.",
            },
        )
        self.assertEqual(EarlyAccessRegistration.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(Notification.objects.count(), 2)
        self.assertEqual(Event.objects.filter(name="early_access.duplicate").count(), 1)
        self.assertEqual(Event.objects.filter(name="early_access.registered").count(), 1)

    def test_duplicate_is_case_insensitive(self) -> None:
        self.post(valid_payload(email="asha@acme.in"))
        response = self.post(valid_payload(email="ASHA@Acme.IN"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.json()["already_registered"])
        self.assertEqual(EarlyAccessRegistration.objects.count(), 1)

    def test_duplicate_reports_existing_status(self) -> None:
        self.post(valid_payload())
        EarlyAccessRegistration.objects.update(status="invited")
        response = self.post(valid_payload())
        self.assertEqual(response.json()["status"], "invited")

    # --- validation -------------------------------------------------------

    def assert_400(self, payload: dict[str, Any], field: str, message: str | None = None) -> None:
        response = self.post(payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.content)
        body = response.json()
        self.assertIn(field, body)
        self.assertIsInstance(body[field], list)
        if message is not None:
            self.assertEqual(body[field], [message])
        self.assertEqual(EarlyAccessRegistration.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_bad_email(self) -> None:
        self.assert_400(valid_payload(email="not-an-email"), "email", "Enter a valid email address.")

    def test_missing_team_size(self) -> None:
        payload = valid_payload()
        payload.pop("team_size")
        self.assert_400(payload, "team_size", "Pick one option.")

    def test_unknown_team_size(self) -> None:
        self.assert_400(valid_payload(team_size="100+"), "team_size", "Pick one option.")

    def test_bad_platform_value(self) -> None:
        self.assert_400(valid_payload(platforms=["windows", "bsd"]), "platforms")

    def test_bad_concern_value(self) -> None:
        self.assert_400(valid_payload(concerns=["usb", 5]), "concerns")

    def test_platforms_must_be_a_list(self) -> None:
        self.assert_400(valid_payload(platforms="windows"), "platforms")

    def test_missing_required_fields(self) -> None:
        response = self.post({})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        body = response.json()
        for field in ("name", "email", "company"):
            self.assertEqual(body[field], ["This field is required."])
        self.assertEqual(body["team_size"], ["Pick one option."])

    def test_blank_name_is_required(self) -> None:
        self.assert_400(valid_payload(name="   "), "name", "This field is required.")

    def test_length_limits(self) -> None:
        self.assert_400(valid_payload(name="x" * 121), "name")
        self.assert_400(valid_payload(company="x" * 161), "company")
        self.assert_400(valid_payload(phone="1" * 33), "phone")

    # --- request metadata & throttling ------------------------------------

    def test_ignores_x_forwarded_for_by_default(self) -> None:
        self.post(valid_payload(), HTTP_X_FORWARDED_FOR="203.0.113.9, 10.0.0.1")
        self.assertEqual(EarlyAccessRegistration.objects.get().ip, "127.0.0.1")

    @override_settings(ZINWE_TRUST_X_FORWARDED_FOR=True)
    def test_uses_first_forwarded_hop_when_trusted(self) -> None:
        self.post(valid_payload(), HTTP_X_FORWARDED_FOR="203.0.113.9, 10.0.0.1")
        self.assertEqual(EarlyAccessRegistration.objects.get().ip, "203.0.113.9")

    def test_user_agent_is_truncated(self) -> None:
        self.post(valid_payload(), HTTP_USER_AGENT="A" * 400)
        self.assertEqual(len(EarlyAccessRegistration.objects.get().user_agent), 255)

    def test_throttled_after_20_per_hour(self) -> None:
        # A spoofed, rotating X-Forwarded-For must not reset the limit.
        for i in range(20):
            response = self.post({}, HTTP_X_FORWARDED_FOR=f"198.51.100.{i}")
            self.assertNotEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(self.post(valid_payload()).status_code, status.HTTP_429_TOO_MANY_REQUESTS)


class AccountTests(APITestCase):
    def register(self, **overrides: Any):
        payload = {"email": "Dev@Zinwe.io", "password": "s3cure-Passphrase!", "full_name": "Dev One"}
        payload.update(overrides)
        return self.client.post("/api/auth/register/", payload, format="json")

    def test_register_returns_token(self) -> None:
        response = self.register()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()
        self.assertTrue(body["token"])
        self.assertEqual(body["user"]["email"], "dev@zinwe.io")
        self.assertEqual(body["user"]["full_name"], "Dev One")
        self.assertTrue(User.objects.get().check_password("s3cure-Passphrase!"))

    def test_register_rejects_weak_password(self) -> None:
        response = self.register(password="123")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("password", response.json())
        self.assertFalse(User.objects.exists())

    def test_register_rejects_duplicate_email(self) -> None:
        self.register()
        response = self.register(email="dev@ZINWE.io")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["email"], ["An account with this email already exists."])

    def test_login_and_me(self) -> None:
        self.register()
        response = self.client.post(
            "/api/auth/login/", {"email": "DEV@zinwe.io", "password": "s3cure-Passphrase!"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        token = response.json()["token"]

        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        me = self.client.get("/api/auth/me/")
        self.assertEqual(me.status_code, status.HTTP_200_OK)
        self.assertEqual(me.json()["email"], "dev@zinwe.io")
        self.assertIsNone(me.json()["active_plan"])

    def test_login_wrong_password(self) -> None:
        self.register()
        response = self.client.post(
            "/api/auth/login/", {"email": "dev@zinwe.io", "password": "nope"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["non_field_errors"], ["Email or password is incorrect."])

    def test_me_requires_auth(self) -> None:
        self.assertEqual(self.client.get("/api/auth/me/").status_code, status.HTTP_401_UNAUTHORIZED)


class PlanTests(APITestCase):
    """Catalogue (portal-v1 §3.6, v1.1) and the older `plans/activate/` endpoint."""

    def setUp(self) -> None:
        self.user = User.objects.create_user("owner@acme.in", "s3cure-Passphrase!")
        self.client.force_authenticate(self.user)

    def test_seeded_plans(self) -> None:
        plans = {p.code: p for p in Plan.objects.all()}
        # Legacy rows are kept (history points at them) but switched off.
        self.assertEqual(set(plans), {"free", "startups", "teams", "businesses", "early-access", "pro"})
        active = list(Plan.objects.filter(is_active=True).order_by("sort_order"))
        self.assertEqual([p.code for p in active], ["free", "startups", "teams", "businesses"])
        self.assertFalse(plans["early-access"].is_active)
        self.assertFalse(plans["pro"].is_active)
        self.assertFalse(plans["early-access"].is_recommended)

        rows = {
            code: (p.name, str(p.price_inr_monthly), p.trial_days, p.max_devices, p.requires_payment,
                   p.is_recommended, p.sort_order)
            for code, p in plans.items() if p.is_active
        }
        self.assertEqual(
            rows,
            {
                "free": ("Free forever", "0.00", 0, 3, False, False, 10),
                "startups": ("Startups", "971.00", 0, 10, True, False, 20),
                "teams": ("Teams", "1799.00", 0, 25, True, False, 25),
                "businesses": ("Businesses", "2834.00", 0, 50, True, True, 30),
            },
        )
        # 0024: Teams sits between Startups and Businesses (owner decision 2026-10-17).
        teams = plans["teams"]
        self.assertEqual(
            (str(teams.list_price_inr_monthly), teams.discount_percent, str(teams.price_usd_monthly),
             str(teams.list_price_usd_monthly), teams.history_days),
            ("2499.00", 28, "22.00", "30.00", 180),
        )
        self.assertEqual(teams.features[0], "Everything in Startups")
        self.assertEqual(len(teams.features), 4)
        self.assertEqual(sorted(teams.modules), sorted(plans["businesses"].modules))
        # Saving it in admin keeps the same price (list price less 28%, to the rupee).
        teams.save()
        teams.refresh_from_db()
        self.assertEqual(str(teams.price_inr_monthly), "1799.00")
        # 0022: every plan has every feature; plans differ by PCs and history kept.
        self.assertEqual(plans["free"].features[0], "Every Zinwe feature")
        self.assertEqual(plans["startups"].features[0], "Everything in Free forever")
        self.assertEqual(plans["businesses"].features[0], "Everything in Teams")
        self.assertEqual(
            {code: (sorted(p.modules), p.history_days) for code, p in plans.items() if p.is_active},
            {"free": (sorted(MODULES), 7), "startups": (sorted(MODULES), 90), "teams": (sorted(MODULES), 180),
             "businesses": (sorted(MODULES), 365)},
        )
        # Pricing (0017): list prices up 35%, then 28% / 30% off — what owners pay follows the list price.
        self.assertEqual(
            {code: (str(p.list_price_inr_monthly), p.discount_percent) for code, p in plans.items() if p.is_active},
            {"free": ("0.00", 0), "startups": ("1349.00", 28), "teams": ("2499.00", 28), "businesses": ("4049.00", 30)},
        )
        for plan in active:
            self.assertTrue(plan.tagline, plan.code)

    def test_price_follows_list_price_and_discount(self) -> None:
        plan = Plan.objects.get(code="startups")
        plan.list_price_inr_monthly = Decimal("1500")
        plan.discount_percent = 20
        plan.save()
        self.assertEqual(plan.price_inr_monthly, Decimal("1200.00"))
        plan.list_price_inr_monthly = Decimal("0")  # no list price: the price is set by hand again
        plan.price_inr_monthly = Decimal("999")
        plan.save()
        self.assertEqual(plan.price_inr_monthly, Decimal("999"))

    def test_plan_list_is_public_and_active_only(self) -> None:
        Plan.objects.create(code="legacy", name="Legacy", is_active=False)
        self.client.force_authenticate(None)
        response = self.client.get("/api/auth/plans/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        codes = [p["code"] for p in response.json()]
        self.assertEqual(codes, ["free", "startups", "teams", "businesses"])  # by sort_order
        for hidden in ("legacy", "early-access", "pro"):
            self.assertNotIn(hidden, codes)

    # The older endpoint still handles free plans with trial_days (the legacy
    # early-access plan); reopen that plan to keep covering it.

    def _reopen_early_access(self) -> None:
        # Its 90-day trial was zeroed by 0007; the legacy-trial tests below need it back.
        Plan.objects.filter(code=Plan.EARLY_ACCESS).update(is_active=True, trial_days=90)

    def _register_early_access(self, email: str = "owner@acme.in") -> EarlyAccessRegistration:
        return EarlyAccessRegistration.objects.create(
            name="Owner", email=email, company="Acme", team_size="1-10"
        )

    def test_activate_free_plan(self) -> None:
        response = self.client.post("/api/auth/plans/activate/", {"plan": "free"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()
        self.assertEqual(body["plan"]["code"], "free")
        self.assertIsNone(body["ends_at"])
        self.assertIsNone(body["days_left"])
        self.assertEqual(self.client.get("/api/auth/me/").json()["active_plan"]["plan"]["code"], "free")

    def test_activate_legacy_trial_plan(self) -> None:
        self._reopen_early_access()
        reg = self._register_early_access()
        response = self.client.post("/api/auth/plans/activate/", {"plan": "early-access"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        activation = PlanActivation.objects.get()
        self.assertTrue(activation.is_active)
        expected_end = timezone.now() + timedelta(days=90)
        self.assertLess(abs((activation.ends_at - expected_end).total_seconds()), 60)
        self.assertEqual(response.json()["plan"]["code"], "early-access")
        self.assertIn(response.json()["days_left"], (89, 90))

        me = self.client.get("/api/auth/me/").json()
        self.assertEqual(me["active_plan"]["plan"]["code"], "early-access")
        reg.refresh_from_db()
        self.assertEqual(reg.status, EarlyAccessRegistration.Status.ACTIVATED)

    def test_early_access_no_longer_requires_registration(self) -> None:
        self._reopen_early_access()
        other = self._register_early_access("someone-else@acme.in")
        response = self.client.post("/api/auth/plans/activate/", {"plan": "early-access"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(PlanActivation.objects.get().plan.code, "early-access")
        other.refresh_from_db()
        self.assertEqual(other.status, EarlyAccessRegistration.Status.PENDING)

    def test_paid_plans_cannot_be_activated_here(self) -> None:
        # Paid plans (and any trial on one) start on the subscription page.
        for code in ("startups", "businesses"):
            with self.subTest(code=code):
                response = self.client.post("/api/auth/plans/activate/", {"plan": code}, format="json")
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(
                    response.json()["plan"],
                    ["This plan needs a payment. Choose it on the subscription page."],
                )
        self.assertFalse(PlanActivation.objects.exists())

    def test_trial_cannot_be_restarted(self) -> None:
        self._reopen_early_access()
        self._register_early_access()
        self.client.post("/api/auth/plans/activate/", {"plan": "early-access"}, format="json")
        self.client.post("/api/auth/plans/activate/", {"plan": "free"}, format="json")
        response = self.client.post("/api/auth/plans/activate/", {"plan": "early-access"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["plan"], ["You've already used this trial."])
        active = PlanActivation.objects.get(user=self.user, is_active=True)
        self.assertEqual(active.plan.code, "free")

    def test_only_one_active_activation(self) -> None:
        self._reopen_early_access()
        self._register_early_access()
        self.client.post("/api/auth/plans/activate/", {"plan": "early-access"}, format="json")
        response = self.client.post("/api/auth/plans/activate/", {"plan": "free"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(response.json()["ends_at"])
        self.assertEqual(PlanActivation.objects.filter(user=self.user).count(), 2)
        active = PlanActivation.objects.get(user=self.user, is_active=True)
        self.assertEqual(active.plan.code, "free")

    def test_cannot_activate_inactive_or_unknown_plan(self) -> None:
        Plan.objects.create(code="legacy", name="Legacy", is_active=False)
        for code in ("legacy", "early-access", "pro", "does-not-exist"):
            response = self.client.post("/api/auth/plans/activate/", {"plan": code}, format="json")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertIn("plan", response.json())
        self.assertFalse(PlanActivation.objects.exists())

    def test_activate_requires_auth(self) -> None:
        self.client.force_authenticate(None)
        response = self.client.post("/api/auth/plans/activate/", {"plan": "free"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class UserManagerTests(APITestCase):
    def test_create_superuser(self) -> None:
        admin = User.objects.create_superuser("Admin@Zinwe.Local", "x-long-password-1")
        self.assertEqual(admin.email, "admin@zinwe.local")
        self.assertTrue(admin.is_staff and admin.is_superuser)

    def test_email_required(self) -> None:
        with self.assertRaises(ValueError):
            User.objects.create_user("", "pw")


class AdminSmokeTests(APITestCase):
    """Every admin changelist/add page renders, and the 'Mark as invited' action works."""

    def setUp(self) -> None:
        self.admin = User.objects.create_superuser("admin@zinwe.local", "x-long-password-1")
        self.client.force_login(self.admin)
        self.reg = EarlyAccessRegistration.objects.create(
            name="A", email="a@x.in", company="Co", team_size="1-10"
        )

    def test_pages_render(self) -> None:
        urls = [
            "/admin/",
            "/admin/z_auth/user/",
            "/admin/z_auth/user/add/",
            f"/admin/z_auth/user/{self.admin.pk}/change/",
            "/admin/z_auth/plan/",
            "/admin/z_auth/planactivation/",
            "/admin/z_auth/earlyaccessregistration/",
            f"/admin/z_auth/earlyaccessregistration/{self.reg.pk}/change/",
            "/admin/z_notification/notification/",
            "/admin/z_analytics/event/",
            "/admin/z_auth/company/",
            "/admin/z_auth/subscription/",
            "/admin/z_auth/manualpayment/",
            "/admin/z_auth/onetimecode/",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_add_user_via_admin(self) -> None:
        response = self.client.post(
            "/admin/z_auth/user/add/",
            {
                "email": "New@Acme.in",
                "full_name": "New Person",
                "usable_password": "true",
                "password1": "s3cure-Passphrase!",
                "password2": "s3cure-Passphrase!",
            },
        )
        self.assertEqual(response.status_code, 302, response.content[:2000])
        self.assertTrue(User.objects.filter(email="new@acme.in").exists())

    def test_mark_as_invited_action(self) -> None:
        response = self.client.post(
            "/admin/z_auth/earlyaccessregistration/",
            {"action": "mark_as_invited", "_selected_action": [self.reg.pk]},
        )
        self.assertEqual(response.status_code, 302)
        self.reg.refresh_from_db()
        self.assertEqual(self.reg.status, "invited")
