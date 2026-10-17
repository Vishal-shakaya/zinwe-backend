"""Sign-up, email codes, sign-in, logout, me and password reset (portal-v1 §3.2-3.4)."""
from __future__ import annotations

from django.core import mail
from django.test import override_settings
from rest_framework import status
from rest_framework.authtoken.models import Token

from z_analytics.models import Event
from z_notification.models import Notification

from ._testing import STRONG_PASSWORD, PortalTestCase, age_codes, codes_sent_to, last_code
from .models import OneTimeCode, User
from z_auth.entitlements import MODULES

SIGNUP = "/api/auth/signup/"
VERIFY = "/api/auth/signup/verify/"
RESEND = "/api/auth/otp/resend/"
LOGIN = "/api/auth/login/"
LOGIN_OTP_REQUEST = "/api/auth/login/otp/request/"
LOGIN_OTP_VERIFY = "/api/auth/login/otp/verify/"
LOGOUT = "/api/auth/logout/"
ME = "/api/auth/me/"
FORGOT = "/api/auth/password/forgot/"
RESET = "/api/auth/password/reset/"

BAD_CODE = {"code": ["That code is incorrect or has expired."]}
RESEND_BODY = {"detail": "If the address can receive a code, a new one is on its way.", "resend_in": 60}
LOGIN_CODE_BODY = {"detail": "If an account exists for this email, we've sent a sign-in code.", "resend_in": 60}
RESET_BODY = {"detail": "If an account exists for this email, we've sent a reset code.", "resend_in": 60}
NOT_VERIFIED = {
    "code": "email_not_verified",
    "detail": "Verify your email to continue. Enter the code we emailed you.",
}
FRESH_ONBOARDING = {"step": "company", "has_company": False, "subscription_status": None}


def signup_payload(**overrides):  # type: ignore[no-untyped-def]
    return {
        "full_name": "Asha Verma", "email": "asha@acme.in", "password": STRONG_PASSWORD, "accept_terms": True,
        **overrides,
    }


class SignupTests(PortalTestCase):
    def test_signup_creates_unverified_user_and_emails_a_code(self) -> None:
        response = self.post(SIGNUP, signup_payload(email="  Asha@ACME.in "))

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        self.assertEqual(response.json(), {"email": "asha@acme.in", "otp_sent": True, "resend_in": 60})
        user = User.objects.get()
        self.assertEqual((user.email, user.full_name), ("asha@acme.in", "Asha Verma"))
        self.assertFalse(user.is_active)
        self.assertIsNone(user.email_verified_at)
        self.assertTrue(user.check_password(STRONG_PASSWORD))

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["asha@acme.in"])
        self.assertEqual(message.subject, "Your Zinwe sign-up code")
        self.assertIn("expires in 10 minutes", message.body)
        self.assertIn("ignore this email", message.body)
        code = last_code("asha@acme.in")
        self.assertIn(code, message.alternatives[0][0])

        row = OneTimeCode.objects.get()
        self.assertEqual(row.purpose, "signup")
        self.assertNotEqual(row.code_hash, code)
        self.assertEqual(len(row.code_hash), 64)

        logged = Notification.objects.get(template="otp_signup")
        self.assertEqual(logged.status, Notification.Status.SENT)
        self.assertNotIn(code, logged.body)
        self.assertIn("•" * 6, logged.body)
        self.assertTrue(Event.objects.filter(name="auth.signup_started").exists())

    def test_validation_errors(self) -> None:
        response = self.post(SIGNUP, {})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        for field in ("full_name", "email", "password"):
            self.assertEqual(response.json()[field], ["This field is required."])

        response = self.post(SIGNUP, signup_payload(email="nope"))
        self.assertEqual(response.json()["email"], ["Enter a valid email address."])

        response = self.post(SIGNUP, signup_payload(password="123"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("password", response.json())
        self.assertFalse(User.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_existing_account_is_rejected(self) -> None:
        self.make_owner("asha@acme.in", login=False)
        response = self.post(SIGNUP, signup_payload(email="ASHA@acme.in"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json(), {"email": ["An account with this email already exists."]})
        self.assertEqual(len(mail.outbox), 0)

    def test_resubmit_for_unverified_updates_and_sends_new_code(self) -> None:
        self.post(SIGNUP, signup_payload())
        first = last_code("asha@acme.in")
        age_codes("asha@acme.in")

        response = self.post(SIGNUP, signup_payload(full_name="Asha V", password="An0ther-Passphrase!"))

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.json()["otp_sent"])
        self.assertEqual(User.objects.count(), 1)
        user = User.objects.get()
        self.assertEqual(user.full_name, "Asha V")
        self.assertTrue(user.check_password("An0ther-Passphrase!"))
        second = last_code("asha@acme.in")
        self.assertEqual(len(codes_sent_to("asha@acme.in")), 2)
        if first != second:
            self.assertEqual(self.post(VERIFY, {"email": "asha@acme.in", "code": first}).json(), BAD_CODE)
        self.assertEqual(self.post(VERIFY, {"email": "asha@acme.in", "code": second}).status_code, 200)

    def test_resubmit_inside_cooldown_keeps_the_earlier_code(self) -> None:
        self.post(SIGNUP, signup_payload())
        response = self.post(SIGNUP, signup_payload(full_name="Asha V"))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()
        self.assertFalse(body["otp_sent"])
        self.assertTrue(1 <= body["resend_in"] <= 60)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(User.objects.get().full_name, "Asha V")
        response = self.post(VERIFY, {"email": "asha@acme.in", "code": last_code("asha@acme.in")})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_signup_is_public_without_csrf(self) -> None:
        self.client.enforce_csrf_checks = True
        self.assertEqual(self.post(SIGNUP, signup_payload()).status_code, status.HTTP_201_CREATED)

    def test_signup_throttled_after_10_per_hour(self) -> None:
        for _ in range(10):
            self.assertNotEqual(self.post(SIGNUP, {}).status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        response = self.post(SIGNUP, signup_payload())
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertIn("detail", response.json())
        self.assertGreater(response.json()["retry_after"], 0)


class SignupVerifyTests(PortalTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.post(SIGNUP, signup_payload())
        self.code = last_code("asha@acme.in")
        mail.outbox.clear()

    def test_verify_activates_and_signs_in(self) -> None:
        response = self.post(VERIFY, {"email": "ASHA@acme.in", "code": self.code})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        body = response.json()
        self.assertEqual(set(body), {"token", "user", "onboarding"})
        user = User.objects.get()
        self.assertEqual(body["token"], Token.objects.get(user=user).key)
        self.assertEqual(
            set(body["user"]),
            {"id", "email", "full_name", "phone", "is_staff", "date_joined", "email_verified", "terms", "tour_done"},
        )
        self.assertTrue(body["user"]["email_verified"])
        self.assertEqual(body["onboarding"], FRESH_ONBOARDING)
        self.assertTrue(user.is_active)
        self.assertIsNotNone(user.email_verified_at)

        self.assertEqual([m.subject for m in mail.outbox], ["Welcome to Zinwe"])
        self.assertTrue(Event.objects.filter(name="auth.signup_verified").exists())

        # The code is single-use.
        self.assertEqual(self.post(VERIFY, {"email": "asha@acme.in", "code": self.code}).json(), BAD_CODE)

    def test_code_with_spaces_is_accepted(self) -> None:
        spaced = f"{self.code[:3]} {self.code[3:]}"
        self.assertEqual(self.post(VERIFY, {"email": "asha@acme.in", "code": spaced}).status_code, 200)

    def test_wrong_or_malformed_code(self) -> None:
        wrong = "000000" if self.code != "000000" else "111111"
        for code in (wrong, "12ab56", "1234", "1234567"):
            with self.subTest(code=code):
                response = self.post(VERIFY, {"email": "asha@acme.in", "code": code})
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(response.json(), BAD_CODE)
        self.assertFalse(User.objects.get().is_active)

    def test_missing_fields(self) -> None:
        response = self.post(VERIFY, {})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["code"], ["This field is required."])
        self.assertEqual(response.json()["email"], ["This field is required."])

    def test_unknown_email(self) -> None:
        response = self.post(VERIFY, {"email": "nobody@acme.in", "code": self.code})
        self.assertEqual(response.json(), BAD_CODE)

    def test_five_wrong_codes_burn_it(self) -> None:
        wrong = "000000" if self.code != "000000" else "111111"
        for _ in range(5):
            self.post(VERIFY, {"email": "asha@acme.in", "code": wrong})
        self.assertEqual(self.post(VERIFY, {"email": "asha@acme.in", "code": self.code}).json(), BAD_CODE)


class ResendTests(PortalTestCase):
    def test_resend_signup_code(self) -> None:
        self.post(SIGNUP, signup_payload())
        age_codes("asha@acme.in")
        response = self.post(RESEND, {"email": "asha@acme.in", "purpose": "signup"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), RESEND_BODY)
        self.assertEqual(len(codes_sent_to("asha@acme.in")), 2)

    def test_unknown_email_gets_the_same_answer_and_no_email(self) -> None:
        response = self.post(RESEND, {"email": "ghost@acme.in", "purpose": "signup"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), RESEND_BODY)
        self.assertEqual(len(mail.outbox), 0)

    def test_verified_account_gets_no_signup_code(self) -> None:
        self.make_owner("asha@acme.in", login=False)
        response = self.post(RESEND, {"email": "asha@acme.in", "purpose": "signup"})
        self.assertEqual(response.json(), RESEND_BODY)
        self.assertEqual(len(mail.outbox), 0)

    def test_cooldown_429_is_the_same_for_known_and_unknown_emails(self) -> None:
        self.make_owner("asha@acme.in", login=False)
        for email in ("asha@acme.in", "ghost@acme.in"):
            with self.subTest(email=email):
                self.assertEqual(self.post(RESEND, {"email": email, "purpose": "login"}).status_code, 200)
                response = self.post(RESEND, {"email": email, "purpose": "login"})
                self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
                body = response.json()
                self.assertEqual(set(body), {"detail", "retry_after"})
                self.assertTrue(1 <= body["retry_after"] <= 60)
                self.assertEqual(response["Retry-After"], str(body["retry_after"]))
        self.assertEqual(len(mail.outbox), 1)  # only the real account got an email

    def test_bad_purpose(self) -> None:
        response = self.post(RESEND, {"email": "asha@acme.in", "purpose": "hack"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json(), {"purpose": ["Pick one option."]})

    def test_otp_scope_throttled_after_30_per_hour(self) -> None:
        for i in range(30):
            self.post(RESEND, {"email": f"u{i}@acme.in", "purpose": "login"})
        response = self.post(LOGIN_OTP_REQUEST, {"email": "x@acme.in"})
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertIn("retry_after", response.json())


class PasswordLoginTests(PortalTestCase):
    def login(self, email: str = "asha@acme.in", password: str = STRONG_PASSWORD):  # type: ignore[no-untyped-def]
        return self.post(LOGIN, {"email": email, "password": password})

    def test_login_returns_auth_response(self) -> None:
        user = self.make_owner("asha@acme.in", login=False)
        response = self.login("ASHA@acme.in")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(body["token"], Token.objects.get(user=user).key)
        self.assertEqual(body["user"]["email"], "asha@acme.in")
        self.assertEqual(body["onboarding"], FRESH_ONBOARDING)
        user.refresh_from_db()
        self.assertIsNotNone(user.last_login)

    def test_wrong_password_and_unknown_email_are_the_same_400(self) -> None:
        self.make_owner("asha@acme.in", login=False)
        for email, password in (("asha@acme.in", "wrong-pass"), ("ghost@acme.in", STRONG_PASSWORD)):
            response = self.login(email, password)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(response.json(), {"non_field_errors": ["Email or password is incorrect."]})

    def test_unverified_account_gets_403_and_a_fresh_code(self) -> None:
        self.post(SIGNUP, signup_payload())
        age_codes("asha@acme.in")
        mail.outbox.clear()

        response = self.login()

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.json(), NOT_VERIFIED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Your Zinwe sign-up code")
        response = self.post(VERIFY, {"email": "asha@acme.in", "code": last_code("asha@acme.in")})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.login().status_code, status.HTTP_200_OK)

    def test_unverified_inside_cooldown_still_403_without_new_email(self) -> None:
        self.post(SIGNUP, signup_payload())
        mail.outbox.clear()
        response = self.login()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.json(), NOT_VERIFIED)
        self.assertEqual(len(mail.outbox), 0)

    def test_unverified_with_wrong_password_is_generic_400(self) -> None:
        self.post(SIGNUP, signup_payload())
        age_codes("asha@acme.in")
        mail.outbox.clear()
        response = self.login(password="wrong-pass")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(len(mail.outbox), 0)

    def test_deactivated_verified_account_cannot_log_in(self) -> None:
        self.make_owner("asha@acme.in", login=False, is_active=False)
        response = self.login()
        # Blocked by staff: told the account is paused (not "wrong password").
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.json()["code"], "account_paused")
        self.assertIn("paused", response.json()["detail"])
        self.assertNotIn("token", response.json())
        self.assertEqual(len(mail.outbox), 0)

    def test_paused_account_with_wrong_password_is_generic_400(self) -> None:
        self.make_owner("asha@acme.in", login=False, is_active=False)
        response = self.login(password="wrong-pass")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)  # doesn't reveal the account

    def test_successful_logins_are_not_throttled(self) -> None:
        """J5-22: only failed attempts count, so an office behind one IP never locks out a right password."""
        self.make_owner("asha@acme.in", login=False)
        for _ in range(25):
            self.assertEqual(self.login().status_code, status.HTTP_200_OK)

    def test_failed_logins_are_counted_per_email(self) -> None:
        self.make_owner("asha@acme.in", login=False)
        self.make_owner("ravi@acme.in", login=False)
        for _ in range(20):
            self.login(password="wrong-pass")
        self.assertEqual(self.login().status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        other = self.login("ravi@acme.in")
        self.assertEqual(other.status_code, status.HTTP_200_OK)  # a colleague on the same IP still gets in

    def test_login_throttled_after_20_per_hour(self) -> None:
        for _ in range(20):
            self.login(password="wrong-pass")
        self.assertEqual(self.login().status_code, status.HTTP_429_TOO_MANY_REQUESTS)


class OtpLoginTests(PortalTestCase):
    def test_request_and_verify(self) -> None:
        user = self.make_owner("asha@acme.in", login=False)
        response = self.post(LOGIN_OTP_REQUEST, {"email": "Asha@acme.in"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), LOGIN_CODE_BODY)
        self.assertEqual(mail.outbox[0].subject, "Your Zinwe sign-in code")
        code = last_code("asha@acme.in")
        self.assertNotIn(code, Notification.objects.get(template="otp_login").body)

        response = self.post(LOGIN_OTP_VERIFY, {"email": "asha@acme.in", "code": code})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(body["token"], Token.objects.get(user=user).key)
        self.assertEqual(body["onboarding"]["step"], "company")
        self.assertTrue(Event.objects.filter(name="auth.login", properties={"method": "otp"}).exists())

    def test_unknown_or_unverified_email_gets_same_body_and_no_email(self) -> None:
        self.post(SIGNUP, signup_payload(email="pending@acme.in"))
        mail.outbox.clear()
        for email in ("ghost@acme.in", "pending@acme.in"):
            response = self.post(LOGIN_OTP_REQUEST, {"email": email})
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.json(), LOGIN_CODE_BODY)
        self.assertEqual(len(mail.outbox), 0)

    def test_wrong_code_and_wrong_purpose(self) -> None:
        self.make_owner("asha@acme.in", login=False)
        self.post(FORGOT, {"email": "asha@acme.in"})
        reset_code = last_code("asha@acme.in")
        response = self.post(LOGIN_OTP_VERIFY, {"email": "asha@acme.in", "code": reset_code})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json(), BAD_CODE)

    def test_otp_login_marks_legacy_account_verified(self) -> None:
        user = self.make_owner("old@acme.in", login=False, email_verified_at=None)
        self.post(LOGIN_OTP_REQUEST, {"email": "old@acme.in"})
        response = self.post(LOGIN_OTP_VERIFY, {"email": "old@acme.in", "code": last_code("old@acme.in")})
        self.assertTrue(response.json()["user"]["email_verified"])
        user.refresh_from_db()
        self.assertIsNotNone(user.email_verified_at)


class LogoutAndMeTests(PortalTestCase):
    def test_logout_deletes_the_token(self) -> None:
        user = self.make_owner()
        response = self.post(LOGOUT)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Token.objects.filter(user=user).exists())
        self.assertEqual(self.get(ME).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_logout_requires_auth(self) -> None:
        self.assertEqual(self.post(LOGOUT).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_me_shape(self) -> None:
        self.make_owner()
        response = self.get(ME)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(
            set(body),
            {
                "id", "email", "full_name", "phone", "is_staff", "date_joined", "email_verified", "terms", "tour_done",
                "onboarding", "company", "subscription", "active_plan", "entitlements",
            },
        )
        self.assertEqual(body["onboarding"], FRESH_ONBOARDING)
        # No active plan yet: Free forever's modules and PC limit apply.
        self.assertEqual(body["entitlements"]["plan_code"], "free")
        self.assertFalse(body["entitlements"]["on_active_plan"])
        # Every plan has every feature (0022); Free differs by PCs and history kept.
        self.assertEqual(body["entitlements"]["modules"], sorted(MODULES))
        self.assertEqual(body["entitlements"]["max_devices"], 3)
        self.assertEqual(body["entitlements"]["history_days"], 7)
        self.assertEqual(body["entitlements"]["devices_used"], 0)
        self.assertEqual(body["entitlements"]["upgrade_for"], {})
        self.assertIsNone(body["company"])
        self.assertIsNone(body["subscription"])
        self.assertIsNone(body["active_plan"])

    def test_me_requires_auth(self) -> None:
        self.assertEqual(self.get(ME).status_code, status.HTTP_401_UNAUTHORIZED)


class PasswordResetTests(PortalTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = self.make_owner("asha@acme.in")
        self.old_token = Token.objects.get(user=self.user).key

    def request_code(self) -> str:
        response = self.post(FORGOT, {"email": "asha@acme.in"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), RESET_BODY)
        return last_code("asha@acme.in")

    def test_reset_revokes_old_tokens_and_signs_in(self) -> None:
        code = self.request_code()
        self.assertEqual(mail.outbox[-1].subject, "Your Zinwe password reset code")
        self.assertNotIn(code, Notification.objects.get(template="otp_password_reset").body)
        self.use_token(None)

        response = self.post(RESET, {"email": "asha@acme.in", "code": code, "new_password": "Br4nd-new-Secret!"})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        body = response.json()
        self.assertNotEqual(body["token"], self.old_token)
        self.assertEqual(body["onboarding"], FRESH_ONBOARDING)
        self.assertFalse(Token.objects.filter(key=self.old_token).exists())

        self.use_token(self.old_token)
        self.assertEqual(self.get(ME).status_code, status.HTTP_401_UNAUTHORIZED)
        self.use_token(body["token"])
        self.assertEqual(self.get(ME).status_code, status.HTTP_200_OK)

        self.use_token(None)
        self.assertEqual(
            self.post(LOGIN, {"email": "asha@acme.in", "password": STRONG_PASSWORD}).status_code, 400
        )
        self.assertEqual(
            self.post(LOGIN, {"email": "asha@acme.in", "password": "Br4nd-new-Secret!"}).status_code, 200
        )
        self.assertTrue(Event.objects.filter(name="auth.password_reset").exists())

    def test_unknown_email_gets_the_same_answer(self) -> None:
        response = self.post(FORGOT, {"email": "ghost@acme.in"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), RESET_BODY)
        self.assertEqual(len(mail.outbox), 0)

    def test_weak_password_does_not_burn_the_code(self) -> None:
        code = self.request_code()
        response = self.post(RESET, {"email": "asha@acme.in", "code": code, "new_password": "123"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("new_password", response.json())
        response = self.post(RESET, {"email": "asha@acme.in", "code": code, "new_password": "Br4nd-new-Secret!"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_wrong_code(self) -> None:
        code = self.request_code()
        wrong = "000000" if code != "000000" else "111111"
        response = self.post(RESET, {"email": "asha@acme.in", "code": wrong, "new_password": "Br4nd-new-Secret!"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json(), BAD_CODE)
        self.assertTrue(Token.objects.filter(key=self.old_token).exists())
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(STRONG_PASSWORD))

    def test_missing_fields(self) -> None:
        response = self.post(RESET, {})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(set(response.json()), {"email", "code", "new_password"})


@override_settings(ZINWE_TEAM_NOTIFY_EMAIL="hello@zinwe.io")
class EarlyAccessThrottleBodyTests(PortalTestCase):
    def test_429_body_has_retry_after(self) -> None:
        for _ in range(20):
            self.post("/api/auth/early-access/", {})
        response = self.post("/api/auth/early-access/", {})
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertGreater(response.json()["retry_after"], 0)


class TermsAcceptanceTests(PortalTestCase):
    def test_signup_requires_and_records_terms(self) -> None:
        from django.conf import settings

        response = self.post(SIGNUP, signup_payload(accept_terms=False))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("accept_terms", response.json())
        payload = signup_payload()
        payload.pop("accept_terms")
        self.assertIn("accept_terms", self.post(SIGNUP, payload).json())

        self.assertEqual(self.post(SIGNUP, signup_payload()).status_code, status.HTTP_201_CREATED)
        user = User.objects.get(email="asha@acme.in")
        self.assertIsNotNone(user.terms_accepted_at)
        self.assertEqual(user.terms_version, settings.ZINWE_LEGAL_VERSION)

    def test_existing_owner_is_asked_once_to_accept_updated_terms(self) -> None:
        from django.conf import settings

        owner = self.make_owner(terms_version="2000-01-01")
        terms = self.get("/api/auth/me/").json()["terms"]
        self.assertTrue(terms["needs_acceptance"])
        self.assertEqual(terms["current_version"], settings.ZINWE_LEGAL_VERSION)

        response = self.post("/api/auth/terms/accept/", {"accept_terms": False})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("accept_terms", response.json())

        response = self.post("/api/auth/terms/accept/", {"accept_terms": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.json()["terms"]["needs_acceptance"])
        owner.refresh_from_db()
        self.assertEqual(owner.terms_version, settings.ZINWE_LEGAL_VERSION)
        self.assertIsNotNone(owner.terms_accepted_at)
        self.assertFalse(self.get("/api/auth/me/").json()["terms"]["needs_acceptance"])

    def test_accepting_terms_needs_sign_in(self) -> None:
        response = self.client.post("/api/auth/terms/accept/", {"accept_terms": True}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
