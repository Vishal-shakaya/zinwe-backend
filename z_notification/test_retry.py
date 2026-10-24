"""FIX-008: failed transactional emails are retried by `run_jobs` with back-off and a cap; rows that died
mid-send are marked failed (and retried when they can be); one-time codes and bulk mail are never retried."""
from __future__ import annotations

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from . import retry
from .channels.email import EmailChannel
from .models import Notification
from .services import notify

CONTEXT = {"first_name": "Asha", "company": "Acme", "trial_days": 90, "platforms_text": "Windows computers"}
TEMPLATE = "early_access_confirmation"  # a transactional email nobody else retries
PDF = ("invoice.pdf", b"%PDF-1.4 invoice", "application/pdf")
REAL_SEND = EmailChannel.send


def failing_once():  # type: ignore[no-untyped-def]
    """EmailChannel.send that fails on the first call and really sends after that."""
    calls = {"n": 0}

    def send(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionRefusedError("smtp down")
        return REAL_SEND(self, *args, **kwargs)

    return send


def later(minutes: float):  # type: ignore[no-untyped-def]
    return timezone.now() + timedelta(minutes=minutes)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", DEFAULT_FROM_EMAIL="Zinwe <hello@zinwe.io>")
class RetryTests(TestCase):
    def fail_first(self, **extra):  # type: ignore[no-untyped-def]
        with mock.patch.object(EmailChannel, "send", failing_once()), self.assertLogs("zinwe.notification", "ERROR"):
            note = notify("email", "asha@acme.in", extra.pop("template", TEMPLATE), CONTEXT, **extra)
        note.refresh_from_db()
        return note

    def test_failed_email_is_retried_whole_and_attempts_recorded(self) -> None:
        note = self.fail_first(
            attachments=[PDF], reply_to=["team@acme.in"], headers={"X-Zinwe": "1"}, ref=("z_auth.payment", 3),
        )
        self.assertEqual(note.status, Notification.Status.FAILED)
        self.assertEqual(note.attempts, 1)
        self.assertIsNotNone(note.next_retry_at)
        self.assertEqual(mail.outbox, [])

        self.assertEqual(retry.run(now=later(1)).sent, 0)  # back-off: not straight away
        result = retry.run(now=later(11))
        self.assertEqual((result.retried, result.sent), (1, 1))
        note.refresh_from_db()
        self.assertEqual(note.status, Notification.Status.SENT)
        self.assertEqual(note.attempts, 2)
        self.assertIsNone(note.retry_payload)  # nothing kept once delivered
        self.assertIsNone(note.next_retry_at)
        self.assertIsNotNone(note.sent_at)
        [msg] = mail.outbox
        self.assertEqual(msg.subject, note.subject)
        self.assertEqual(msg.to, ["asha@acme.in"])
        self.assertEqual(msg.reply_to, ["team@acme.in"])
        self.assertEqual(msg.extra_headers.get("X-Zinwe"), "1")
        self.assertEqual(msg.attachments[0][:2], ("invoice.pdf", b"%PDF-1.4 invoice"))
        self.assertTrue(msg.alternatives)  # the HTML version too
        self.assertEqual(retry.run(now=later(600)).retried, 0)  # never twice

    def test_gives_up_after_the_last_attempt(self) -> None:
        note = self.fail_first()
        with mock.patch.object(EmailChannel, "send", side_effect=ConnectionRefusedError("still down")):
            for _ in range(retry.MAX_ATTEMPTS + 2):
                if note.next_retry_at is None:
                    break
                retry.run(now=note.next_retry_at + timedelta(seconds=1))
                note.refresh_from_db()
        self.assertEqual(note.status, Notification.Status.FAILED)
        self.assertEqual(note.attempts, retry.MAX_ATTEMPTS)
        self.assertIsNone(note.next_retry_at)
        self.assertIsNone(note.retry_payload)
        self.assertIn("still down", note.error)

    def test_backoff_grows(self) -> None:
        note = self.fail_first()
        first_wait = note.next_retry_at - note.created_at
        with mock.patch.object(EmailChannel, "send", side_effect=ConnectionRefusedError("down")):
            retry.run(now=note.next_retry_at + timedelta(seconds=1))
        note.refresh_from_db()
        self.assertEqual(note.attempts, 2)
        self.assertGreater(note.next_retry_at - timezone.now(), first_wait)
        self.assertIn("Attempt 2 of", note.error)

    def test_one_time_codes_are_never_retried(self) -> None:
        from z_auth.services import send_code_email

        with mock.patch.object(EmailChannel, "send", side_effect=ConnectionRefusedError("down")), self.assertLogs("zinwe.notification", "ERROR"):
            send_code_email("asha@acme.in", "login", "123456")
        note = Notification.objects.get(template="otp_login")
        self.assertEqual(note.status, Notification.Status.FAILED)
        self.assertIsNone(note.next_retry_at)
        self.assertIsNone(note.retry_payload)
        self.assertNotIn("123456", str(note.__dict__))
        self.assertEqual(retry.run(now=later(60 * 24)).retried, 0)
        self.assertEqual(mail.outbox, [])

    def test_anything_redacted_or_bulk_or_caller_handled_is_not_retried(self) -> None:
        self.fail_first(redact=["Asha"])
        self.fail_first(fallback=False)  # newsletter / series / guide mail: their own claim rows retry them
        self.fail_first(template="renewal_reminder")  # z_auth.renewals deletes its claim and tries again itself
        self.fail_first(retry=False)
        self.assertFalse(Notification.objects.filter(next_retry_at__isnull=False).exists())
        self.assertFalse(Notification.objects.filter(retry_payload__isnull=False).exists())
        self.assertEqual(retry.run(now=later(60 * 24)).retried, 0)

    def test_rows_stuck_mid_send_are_failed_and_retried_when_possible(self) -> None:
        # A worker killed during the SMTP call: the row stays "queued" with what it was about to send.
        with mock.patch.object(EmailChannel, "send", side_effect=SystemExit("worker killed")):
            with self.assertRaises(SystemExit):
                notify("email", "asha@acme.in", TEMPLATE, CONTEXT)
        stuck = Notification.objects.get()
        self.assertEqual(stuck.status, Notification.Status.QUEUED)
        fresh = Notification.objects.create(channel="email", recipient="new@acme.in", template=TEMPLATE)  # in flight now
        no_payload = Notification.objects.create(channel="email", recipient="code@acme.in", template="otp_login")
        Notification.objects.filter(pk__in=[stuck.pk, no_payload.pk]).update(created_at=timezone.now() - timedelta(hours=1))

        result = retry.run()
        self.assertEqual(result.stuck, 2)
        stuck.refresh_from_db()
        no_payload.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(fresh.status, Notification.Status.QUEUED)  # still inside the safe window
        self.assertEqual(no_payload.status, Notification.Status.FAILED)
        self.assertIn("Interrupted", no_payload.error)
        self.assertEqual(stuck.status, Notification.Status.SENT)  # retried in the same round
        self.assertEqual(len(mail.outbox), 1)

    def test_retry_run_never_raises_on_a_bad_row(self) -> None:
        note = self.fail_first()
        Notification.objects.filter(pk=note.pk).update(retry_payload={"attachments": "not a list"})
        result = retry.run(now=later(11))
        note.refresh_from_db()
        self.assertEqual(result.failed, 1)
        self.assertEqual(note.status, Notification.Status.FAILED)

    def test_run_jobs_runs_the_retries(self) -> None:
        out = StringIO()
        call_command("run_jobs", "--once", stdout=out, stderr=StringIO())
        self.assertIn("email retries:", out.getvalue())
