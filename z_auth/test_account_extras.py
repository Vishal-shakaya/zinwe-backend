"""Signed-in password change (POST password/change/) and the "How are we doing?" emails."""
from __future__ import annotations

from datetime import timedelta
from io import StringIO

from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from z_notification.models import Notification

from .feedback_requests import TEMPLATE, send_feedback_requests
from .models import Feedback, User

CHANGE = "/api/auth/password/change/"
OLD = "old-Passw0rd-123"
NEW = "new-Passw0rd-456"


class PasswordChangeTests(APITestCase):
    def setUp(self) -> None:
        cache.clear()  # throttle counters
        self.user = User.objects.create_user("owner@acme.in", OLD, email_verified_at=timezone.now())
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")

    def post(self, current: str, new: str):  # type: ignore[no-untyped-def]
        return self.client.post(CHANGE, {"current_password": current, "new_password": new}, format="json")

    def test_change_rotates_token_and_returns_auth(self) -> None:
        response = self.post(OLD, NEW)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        body = response.json()
        self.assertEqual(body["user"]["email"], "owner@acme.in")
        self.assertNotEqual(body["token"], self.token.key)  # every other device is signed out
        self.assertFalse(Token.objects.filter(key=self.token.key).exists())
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW))

    def test_wrong_current_password(self) -> None:
        response = self.post("not-it-123", NEW)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("current_password", response.json())
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(OLD))

    def test_weak_or_same_new_password_rejected(self) -> None:
        for bad in ("123", OLD):
            with self.subTest(new=bad):
                response = self.post(OLD, bad)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("new_password", response.json())
        self.assertTrue(Token.objects.filter(key=self.token.key).exists())

    def test_requires_sign_in(self) -> None:
        self.client.credentials()
        self.assertEqual(self.post(OLD, NEW).status_code, status.HTTP_401_UNAUTHORIZED)


@override_settings(
    ZINWE_APP_URL="https://app.zinwe.test",
    ZINWE_FEEDBACK_REQUEST_AFTER_DAYS=14,
    ZINWE_FEEDBACK_REQUEST_EVERY_DAYS=90,
)
class FeedbackRequestTests(TestCase):
    def setUp(self) -> None:
        self.now = timezone.now()
        self.owner = self.make("asha@acme.in", days_old=20)

    def make(self, email: str, days_old: int, **extra) -> User:  # type: ignore[no-untyped-def]
        return User.objects.create_user(
            email, "pw-12345678", full_name="Asha Verma", email_verified_at=self.now,
            date_joined=self.now - timedelta(days=days_old), **extra,
        )

    def test_asks_once_with_rating_links(self) -> None:
        self.assertEqual(send_feedback_requests(now=self.now).sent, ["asha@acme.in"])
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertIn("https://app.zinwe.test/app/feedback", email.body)
        html = email.alternatives[0][0]
        self.assertIn("https://app.zinwe.test/app/feedback?rating=10", html)
        # Asked recently: not again on the next run.
        self.assertEqual(send_feedback_requests(now=self.now + timedelta(days=1)).sent, [])
        # ...but again once the cool-down has passed.
        self.assertEqual(send_feedback_requests(now=self.now + timedelta(days=91)).sent, ["asha@acme.in"])

    def test_skips_new_staff_unverified_and_recent_feedback(self) -> None:
        self.make("new@acme.in", days_old=3)
        self.make("staff@zinwe.io", days_old=40, is_staff=True)
        User.objects.create_user("pending@acme.in", "pw-12345678", date_joined=self.now - timedelta(days=40))
        rated = self.make("rated@acme.in", days_old=40)
        Feedback.objects.create(owner=rated, rating=9, created_at=self.now - timedelta(days=5))
        self.assertEqual(send_feedback_requests(now=self.now).sent, ["asha@acme.in"])

    def test_failed_send_is_retried(self) -> None:
        Notification.objects.create(
            channel="email", recipient="asha@acme.in", template=TEMPLATE, status=Notification.Status.FAILED
        )
        self.assertEqual(send_feedback_requests(now=self.now).sent, ["asha@acme.in"])

    def test_command_dry_run_sends_nothing(self) -> None:
        out = StringIO()
        call_command("send_feedback_requests", "--dry-run", stdout=out)
        self.assertIn("Would send feedback request -> asha@acme.in", out.getvalue())
        self.assertEqual(len(mail.outbox), 0)
