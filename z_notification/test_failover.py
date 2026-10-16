"""FailoverEmailBackend: primary SMTP first, the fallback when the primary fails."""
from __future__ import annotations

import smtplib
from unittest import mock

from django.core.mail import EmailMessage, get_connection
from django.test import SimpleTestCase, TestCase, override_settings

from . import backends
from .models import Notification
from .services import notify

PRIMARY_HOST = "smtp-relay.brevo.com"
FALLBACK_HOST = "smtp.gmail.com"

FAILOVER = override_settings(
    EMAIL_BACKEND="z_notification.backends.FailoverEmailBackend",
    DEFAULT_FROM_EMAIL="Zinwe <hello@zinwe.io>",
    EMAIL_HOST=PRIMARY_HOST, EMAIL_PORT=587, EMAIL_HOST_USER="brevo-login", EMAIL_HOST_PASSWORD="k", EMAIL_USE_TLS=True,
    EMAIL_FALLBACK_HOST=FALLBACK_HOST, EMAIL_FALLBACK_PORT=587, EMAIL_FALLBACK_HOST_USER="team@gmail.com",
    EMAIL_FALLBACK_HOST_PASSWORD="g", EMAIL_FALLBACK_USE_TLS=True, EMAIL_FALLBACK_FROM="Zinwe <team@gmail.com>",
    EMAIL_FAILOVER_PAUSE_SECONDS=600,
)


class FakeSMTP:
    """Stands in for django's SMTP backend: records what each host was asked to send."""

    calls: list[tuple[str, str, list[str], list[str]]] = []
    failing: set[str] = set()

    def __init__(self, host: str = "", **kwargs: object) -> None:
        self.host = host

    def send_messages(self, messages: list[EmailMessage]) -> int:
        for m in messages:
            FakeSMTP.calls.append((self.host, m.from_email, list(m.to), list(m.reply_to)))
        if self.host in FakeSMTP.failing:
            raise smtplib.SMTPAuthenticationError(525, b"5.7.1 Unauthorized IP address")
        return len(messages)


@FAILOVER
class FailoverTests(SimpleTestCase):
    def setUp(self) -> None:
        backends.reset_pause()
        FakeSMTP.calls, FakeSMTP.failing = [], set()
        patcher = mock.patch.object(backends, "SMTPBackend", FakeSMTP)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(backends.reset_pause)

    def send(self, **extra: object) -> int:
        msg = EmailMessage("Your code", "123456", to=["owner@acme.in"], **extra)
        return get_connection().send_messages([msg])

    def test_primary_sends_when_healthy(self) -> None:
        self.assertEqual(self.send(), 1)
        self.assertEqual(FakeSMTP.calls, [(PRIMARY_HOST, "Zinwe <hello@zinwe.io>", ["owner@acme.in"], [])])

    def test_primary_failure_goes_to_fallback_from_gmail_with_reply_to(self) -> None:
        FakeSMTP.failing = {PRIMARY_HOST}
        with self.assertLogs("zinwe.notification.email", "WARNING"):
            self.assertEqual(self.send(), 1)
        self.assertEqual(FakeSMTP.calls[1], (FALLBACK_HOST, "Zinwe <team@gmail.com>", ["owner@acme.in"], ["Zinwe <hello@zinwe.io>"]))

    def test_existing_reply_to_is_kept(self) -> None:
        FakeSMTP.failing = {PRIMARY_HOST}
        with self.assertLogs("zinwe.notification.email", "WARNING"):
            self.send(reply_to=["support@zinwe.io"])
        self.assertEqual(FakeSMTP.calls[1][3], ["support@zinwe.io"])

    def test_primary_is_skipped_while_paused_then_retried(self) -> None:
        FakeSMTP.failing = {PRIMARY_HOST}
        with self.assertLogs("zinwe.notification.email", "WARNING"):
            self.send()
        FakeSMTP.failing = set()  # primary healthy again, but still paused
        FakeSMTP.calls = []
        self.send()
        self.assertEqual([c[0] for c in FakeSMTP.calls], [FALLBACK_HOST])
        backends.reset_pause()  # pause over
        FakeSMTP.calls = []
        self.send()
        self.assertEqual([c[0] for c in FakeSMTP.calls], [PRIMARY_HOST])

    def test_both_failing_raises_unless_silent(self) -> None:
        FakeSMTP.failing = {PRIMARY_HOST, FALLBACK_HOST}
        with self.assertLogs("zinwe.notification.email", "WARNING"), self.assertRaises(smtplib.SMTPAuthenticationError):
            self.send()
        msg = EmailMessage("x", "y", to=["a@b.in"])
        self.assertEqual(get_connection(fail_silently=True).send_messages([msg]), 0)

    @override_settings(EMAIL_FALLBACK_HOST="")
    def test_no_fallback_configured_raises_the_primary_error(self) -> None:
        FakeSMTP.failing = {PRIMARY_HOST}
        with self.assertLogs("zinwe.notification.email", "WARNING"), self.assertRaises(smtplib.SMTPAuthenticationError):
            self.send()

    def test_message_is_restored_after_fallback(self) -> None:
        FakeSMTP.failing = {PRIMARY_HOST}
        msg = EmailMessage("x", "y", to=["a@b.in"])
        with self.assertLogs("zinwe.notification.email", "WARNING"):
            get_connection().send_messages([msg])
        self.assertEqual((msg.from_email, msg.reply_to), ("Zinwe <hello@zinwe.io>", []))


@FAILOVER
class NotifyThroughFailoverTests(TestCase):
    """The app's real path: notify() -> EmailChannel -> FailoverEmailBackend."""

    def setUp(self) -> None:
        backends.reset_pause()
        FakeSMTP.calls, FakeSMTP.failing = [], {PRIMARY_HOST}
        patcher = mock.patch.object(backends, "SMTPBackend", FakeSMTP)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(backends.reset_pause)

    def test_notification_is_sent_and_logged_when_only_the_fallback_works(self) -> None:
        with self.assertLogs("zinwe.notification.email", "WARNING"):
            note = notify("email", "owner@acme.in", "otp_signup", {"first_name": "Asha", "code": "123456"})
        self.assertEqual(note.status, Notification.Status.SENT)
        self.assertEqual([c[0] for c in FakeSMTP.calls], [PRIMARY_HOST, FALLBACK_HOST])
