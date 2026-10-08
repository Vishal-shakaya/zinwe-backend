from __future__ import annotations

from unittest import mock

from django.core import mail
from django.test import TestCase

from .channels import Channel, get_channel, register_channel, registered_channels
from .channels import _REGISTRY
from .channels.email import EmailChannel
from .models import Notification
from .services import notify

CONTEXT = {
    "first_name": "Asha",
    "company": "Acme",
    "trial_days": 90,
    "platforms_text": "Windows computers",
}


class RecordingChannel(Channel):
    name = "test-recorder"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str, str | None]] = []

    def send(self, recipient: str, subject: str, body: str, html: str | None = None) -> None:
        self.sent.append((recipient, subject, body, html))


class NotifyTests(TestCase):
    def test_email_is_rendered_logged_and_sent(self) -> None:
        notification = notify("email", "asha@acme.in", "early_access_confirmation", CONTEXT, ref=("z_auth.earlyaccessregistration", 7))
        self.assertIsNotNone(notification)
        notification.refresh_from_db()
        self.assertEqual(notification.status, Notification.Status.SENT)
        self.assertIsNotNone(notification.sent_at)
        self.assertEqual(notification.ref_type, "z_auth.earlyaccessregistration")
        self.assertEqual(notification.ref_id, "7")
        self.assertEqual(notification.subject, "You're on the Zinwe early-access list")
        self.assertIn("Hi Asha", notification.body)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].from_email, "Zinwe <hello@zinwe.io>")

    def test_channel_failure_is_logged_not_raised(self) -> None:
        with (
            mock.patch.object(EmailChannel, "send", side_effect=RuntimeError("smtp down")),
            self.assertLogs("zinwe.notification", "ERROR"),
        ):
            notification = notify("email", "asha@acme.in", "early_access_confirmation", CONTEXT)
        notification.refresh_from_db()
        self.assertEqual(notification.status, Notification.Status.FAILED)
        self.assertIn("smtp down", notification.error)
        self.assertIsNone(notification.sent_at)

    def test_unknown_channel_is_logged_not_raised(self) -> None:
        with self.assertLogs("zinwe.notification", "ERROR"):
            notification = notify("carrier-pigeon", "x@y.z", "early_access_confirmation", CONTEXT)
        self.assertEqual(notification.status, Notification.Status.FAILED)
        self.assertIn("carrier-pigeon", notification.error)

    def test_missing_template_is_logged_not_raised(self) -> None:
        with self.assertLogs("zinwe.notification", "ERROR"):
            notification = notify("email", "x@y.z", "no_such_template", {})
        self.assertEqual(notification.status, Notification.Status.FAILED)
        self.assertEqual(len(mail.outbox), 0)

    def test_custom_channel_can_be_registered(self) -> None:
        channel = RecordingChannel()
        register_channel(channel)
        try:
            self.assertIn("test-recorder", registered_channels())
            self.assertIs(get_channel("test-recorder"), channel)
            notify("test-recorder", "+919810000000", "early_access_confirmation", CONTEXT)
            self.assertEqual(len(channel.sent), 1)
            self.assertEqual(channel.sent[0][0], "+919810000000")
            with self.assertRaises(ValueError):
                register_channel(RecordingChannel())
        finally:
            _REGISTRY.pop("test-recorder", None)


class TeamAlertTemplateTests(TestCase):
    def test_team_alert_renders_registration(self) -> None:
        from z_auth.models import EarlyAccessRegistration

        reg = EarlyAccessRegistration.objects.create(
            name="Asha", email="asha@acme.in", company="Acme & Sons", team_size="1-10"
        )
        notification = notify(
            "email", "hello@zinwe.io", "early_access_team_alert",
            {"registration": reg, "platforms_text": "Windows", "concerns_text": "Compliance"},
        )
        self.assertEqual(notification.status, Notification.Status.SENT)
        self.assertIn("Acme & Sons", notification.subject)  # no HTML escaping in text mail
        self.assertIn("asha@acme.in", notification.body)
