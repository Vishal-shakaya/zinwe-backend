"""OTP rules (portal-v1 §2): hashing, expiry, attempts, cooldown, hourly cap, invalidation."""
from __future__ import annotations

import hashlib
import hmac
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.test import TestCase
from django.utils import timezone

from . import otp
from .models import OneTimeCode

EMAIL = "asha@acme.in"
SIGNUP = OneTimeCode.Purpose.SIGNUP
LOGIN = OneTimeCode.Purpose.LOGIN


def at(moment):  # type: ignore[no-untyped-def]
    """Freeze `timezone.now()` (as used by z_auth.otp) at `moment`."""
    return mock.patch("django.utils.timezone.now", return_value=moment)


class OtpTests(TestCase):
    def age(self, seconds: int) -> None:
        for row in OneTimeCode.objects.all():
            row.created_at -= timedelta(seconds=seconds)
            row.save(update_fields=["created_at"])

    # --- storage ----------------------------------------------------------

    def test_code_is_six_digits_and_stored_hashed(self) -> None:
        code = otp.issue_code(EMAIL, SIGNUP, ip="127.0.0.1")
        self.assertRegex(code, r"^\d{6}$")
        row = OneTimeCode.objects.get()
        expected = hmac.new(
            settings.SECRET_KEY.encode(), f"signup:{EMAIL}:{code}".encode(), hashlib.sha256
        ).hexdigest()
        self.assertEqual(row.code_hash, expected)
        self.assertNotIn(code, row.code_hash)
        self.assertEqual((row.email, row.purpose, row.attempts, row.ip), (EMAIL, "signup", 0, "127.0.0.1"))
        self.assertIsNone(row.consumed_at)

    def test_codes_keep_leading_zeros(self) -> None:
        with mock.patch("z_auth.otp.secrets.randbelow", return_value=42):
            self.assertEqual(otp.generate_code(), "000042")

    def test_email_is_normalised(self) -> None:
        code = otp.issue_code("  Asha@ACME.in ", SIGNUP)
        self.assertEqual(OneTimeCode.objects.get().email, EMAIL)
        self.assertTrue(otp.verify_code("ASHA@acme.in", SIGNUP, code))

    # --- verification -----------------------------------------------------

    def test_code_works_once(self) -> None:
        code = otp.issue_code(EMAIL, SIGNUP)
        self.assertTrue(otp.verify_code(EMAIL, SIGNUP, code))
        self.assertIsNotNone(OneTimeCode.objects.get().consumed_at)
        self.assertFalse(otp.verify_code(EMAIL, SIGNUP, code))

    def test_code_is_bound_to_purpose_and_email(self) -> None:
        code = otp.issue_code(EMAIL, SIGNUP)
        self.assertFalse(otp.verify_code(EMAIL, LOGIN, code))
        self.assertFalse(otp.verify_code("other@acme.in", SIGNUP, code))
        self.assertTrue(otp.verify_code(EMAIL, SIGNUP, code))

    def test_expires_after_ten_minutes(self) -> None:
        start = timezone.now()
        with at(start):
            code = otp.issue_code(EMAIL, SIGNUP)
        with at(start + timedelta(minutes=10, seconds=1)):
            self.assertFalse(otp.verify_code(EMAIL, SIGNUP, code))

    def test_still_valid_just_before_expiry(self) -> None:
        start = timezone.now()
        with at(start):
            code = otp.issue_code(EMAIL, SIGNUP)
        with at(start + timedelta(minutes=9, seconds=59)):
            self.assertTrue(otp.verify_code(EMAIL, SIGNUP, code))

    def test_five_wrong_attempts_burn_the_code(self) -> None:
        code = otp.issue_code(EMAIL, SIGNUP)
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            self.assertFalse(otp.verify_code(EMAIL, SIGNUP, wrong))
        self.assertEqual(OneTimeCode.objects.get().attempts, 5)
        self.assertFalse(otp.verify_code(EMAIL, SIGNUP, code))

    def test_four_wrong_attempts_still_allow_the_right_code(self) -> None:
        code = otp.issue_code(EMAIL, SIGNUP)
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(4):
            otp.verify_code(EMAIL, SIGNUP, wrong)
        self.assertTrue(otp.verify_code(EMAIL, SIGNUP, code))

    def test_new_code_invalidates_older_ones(self) -> None:
        first = otp.issue_code(EMAIL, SIGNUP)
        self.age(61)
        second = otp.issue_code(EMAIL, SIGNUP)
        if first != second:
            self.assertFalse(otp.verify_code(EMAIL, SIGNUP, first))
        self.assertTrue(otp.verify_code(EMAIL, SIGNUP, second))

    def test_new_code_leaves_other_purposes_alone(self) -> None:
        login_code = otp.issue_code(EMAIL, LOGIN)
        otp.issue_code(EMAIL, SIGNUP)
        self.assertTrue(otp.verify_code(EMAIL, LOGIN, login_code))

    # --- send limits ------------------------------------------------------

    def test_resend_cooldown_is_60_seconds(self) -> None:
        otp.issue_code(EMAIL, SIGNUP)
        with self.assertRaises(otp.OtpThrottled) as ctx:
            otp.issue_code(EMAIL, SIGNUP)
        self.assertTrue(55 <= ctx.exception.retry_after <= 60, ctx.exception.retry_after)
        self.assertIn("Please wait", str(ctx.exception))
        self.assertEqual(OneTimeCode.objects.count(), 1)
        self.age(61)
        otp.issue_code(EMAIL, SIGNUP)  # fine after the cooldown
        self.assertEqual(OneTimeCode.objects.count(), 2)

    def test_cooldown_is_per_purpose(self) -> None:
        otp.issue_code(EMAIL, SIGNUP)
        otp.issue_code(EMAIL, LOGIN)  # no error
        self.assertEqual(OneTimeCode.objects.count(), 2)

    def test_at_most_five_sends_per_hour(self) -> None:
        for _ in range(5):
            otp.issue_code(EMAIL, SIGNUP)
            self.age(5 * 60)  # 5 minutes between sends
        with self.assertRaises(otp.OtpThrottled) as ctx:
            otp.issue_code(EMAIL, SIGNUP)
        # The oldest of the 5 was sent 25 min ago -> it leaves the window in ~35 min.
        self.assertTrue(34 * 60 <= ctx.exception.retry_after <= 35 * 60, ctx.exception.retry_after)
        self.assertIn("Too many codes", str(ctx.exception))
        self.age(36 * 60)
        otp.issue_code(EMAIL, SIGNUP)  # window has room again

    def test_old_rows_are_cleaned_up(self) -> None:
        otp.issue_code(EMAIL, SIGNUP)
        self.age(2 * 24 * 3600)
        otp.issue_code("other@acme.in", SIGNUP)
        self.assertEqual(list(OneTimeCode.objects.values_list("email", flat=True)), ["other@acme.in"])
