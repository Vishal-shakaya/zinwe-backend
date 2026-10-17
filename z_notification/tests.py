from __future__ import annotations

from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings

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


@override_settings(DEFAULT_FROM_EMAIL="Zinwe <hello@zinwe.io>")  # independent of the local .env
class NotifyTests(TestCase):
    def test_email_is_rendered_logged_and_sent(self) -> None:
        notification = notify("email", "asha@acme.in", "early_access_confirmation", CONTEXT, ref=("z_auth.earlyaccessregistration", 7))
        self.assertIsNotNone(notification)
        notification.refresh_from_db()
        self.assertEqual(notification.status, Notification.Status.SENT)
        self.assertIsNotNone(notification.sent_at)
        self.assertEqual(notification.ref_type, "z_auth.earlyaccessregistration")
        self.assertEqual(notification.ref_id, "7")
        self.assertEqual(notification.subject, "We'll call you to set up Zinwe")
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


class RedactTests(TestCase):
    def test_redacted_values_are_sent_but_not_logged(self) -> None:
        notification = notify(
            "email", "asha@acme.in", "otp_login",
            {"code": "482913", "first_name": "Asha", "minutes": 10},
            redact=["482913"],
        )
        notification.refresh_from_db()
        self.assertEqual(notification.status, Notification.Status.SENT)
        self.assertNotIn("482913", notification.body)
        self.assertNotIn("482913", notification.subject)
        self.assertIn("•" * 6, notification.body)
        self.assertIn("482913", mail.outbox[0].body)
        self.assertIn("482913", mail.outbox[0].alternatives[0][0])

    def test_failure_error_is_redacted_too(self) -> None:
        with (
            mock.patch.object(EmailChannel, "send", side_effect=RuntimeError("bad code 482913")),
            self.assertLogs("zinwe.notification", "ERROR"),
        ):
            notification = notify(
                "email", "asha@acme.in", "otp_login",
                {"code": "482913", "first_name": "Asha", "minutes": 10},
                redact=["482913"],
            )
        notification.refresh_from_db()
        self.assertEqual(notification.status, Notification.Status.FAILED)
        self.assertNotIn("482913", notification.error + notification.body)

    def test_empty_redact_values_are_ignored(self) -> None:
        notification = notify("email", "x@y.z", "early_access_confirmation", CONTEXT, redact=["", None])
        self.assertIn("Hi Asha", notification.body)


class EmailLogoTests(TestCase):
    """Every HTML email shows the Zinwe wordmark: in text by default, the hosted image when configured.
    Nothing is ever attached (an inline cid logo made Gmail drop the emails)."""

    def send(self):  # type: ignore[no-untyped-def]
        from django.core import mail as django_mail

        from z_notification.services import notify

        notify("email", "asha@acme.in", "welcome", {"first_name": "Asha"})
        return django_mail.outbox[0]

    def test_text_wordmark_and_no_attachment_by_default(self) -> None:
        message = self.send()
        html = message.alternatives[0][0]
        self.assertIn('<span style="color:#FD6D02;">Z</span>inwe', html)
        self.assertNotIn("<img", html)
        self.assertNotIn("cid:", html)
        self.assertEqual(message.attachments, [])

    @override_settings(ZINWE_EMAIL_LOGO_URL="https://zinwe.io/brand/zinwe-logo-96.png")
    def test_hosted_logo_url_is_linked(self) -> None:
        message = self.send()
        self.assertIn('src="https://zinwe.io/brand/zinwe-logo-96.png"', message.alternatives[0][0])
        self.assertEqual(message.attachments, [])

    def test_every_html_template_shows_the_logo(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parent / "templates" / "z_notification"
        missing = [p.parent.name for p in root.glob("*/body.html") if "_brand.html" not in p.read_text(encoding="utf-8")]
        self.assertEqual(missing, [])


class PlainTextIsNeverEscapedTests(TestCase):
    """J1-12 / J5-08: "&" in a company name must reach the inbox as "&", in every subject and text body."""

    COMPANY = "Kulkarni & Associates"

    def test_member_invite_subject_and_body(self) -> None:
        from .services import render_template

        subject, body, html = render_template(
            "member_invite",
            {"company": self.COMPANY, "first_name": "Sneha", "member_email": "s@k.in", "member_id": "ZM-1",
             "download_url": "https://x"},
        )
        self.assertEqual(subject, "Kulkarni & Associates added you to Zinwe — connect your PC")
        self.assertIn("Kulkarni & Associates uses Zinwe", body)
        self.assertIn("Kulkarni &amp; Associates", html)  # the HTML part is still escaped

    def test_a_subject_without_an_autoescape_tag_is_not_escaped(self) -> None:
        from django.template import engines

        from .services import render_unescaped

        template = engines["django"].from_string("{{ company }} added you <{{ who }}>")
        self.assertEqual(
            render_unescaped(template, {"company": self.COMPANY, "who": "a\"b'c"}),
            "Kulkarni & Associates added you <a\"b'c>",
        )

    def test_every_subject_renders_ampersands_plainly(self) -> None:
        import os

        from .services import render_plain

        base = os.path.join(os.path.dirname(__file__), "templates", "z_notification")
        folders = [d for d in os.listdir(base) if os.path.isfile(os.path.join(base, d, "subject.txt"))]
        self.assertGreater(len(folders), 20)

        class Anything(str):
            """Any attribute / item lookup gives the company name back, so every {{ x.y }} renders."""

            def __getattr__(self, name: str) -> "Anything":
                if name.startswith("__"):
                    raise AttributeError(name)
                return self

            def __getitem__(self, key: object) -> "Anything":
                return self

        value = Anything(self.COMPANY)
        for folder in folders:
            with self.subTest(template=folder):
                text = render_plain(f"z_notification/{folder}/subject.txt", {
                    "company": value, "subject": value, "plan": value, "registration": value, "ticket": value,
                    "message": value, "doc": value, "name": value, "member_name": value, "titles": [value],
                    "first_name": value, "label": value, "amount_text": value, "topic": value, "payment": value,
                    "owner": value, "total": value, "hostname": value, "count": 1, "days_left": 3,
                })
                self.assertNotIn("&amp;", text)
