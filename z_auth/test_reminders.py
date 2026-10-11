"""Automatic renewal reminders (z_auth.renewals) and the commands that run them."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from z_notification.models import Notification

from .billing import add_months
from .models import Company, ManualPayment, Plan, RenewalReminder, Subscription, User
from .renewals import send_due_reminders


@override_settings(ZINWE_APP_URL="https://app.zinwe.test", ZINWE_RENEWAL_REMINDER_DAYS=(7, 1))
class ReminderTests(TestCase):
    def setUp(self) -> None:
        self.now = timezone.now()
        self.owner = User.objects.create_user("asha@acme.in", "pw-12345678", full_name="Asha Verma")
        Company.objects.create(owner=self.owner, name="Acme", industry="retail", team_size="11-25",
                               phone="+91 90000 00000", city="Delhi", state="Delhi")
        self.plan = Plan.objects.get(code=Plan.STARTUPS)

    def sub(self, days: float, **extra) -> Subscription:  # type: ignore[no-untyped-def]
        defaults = {"status": "active", "amount_inr": Decimal("999.00"), "starts_at": self.now - timedelta(days=20)}
        return Subscription.objects.create(
            owner=self.owner, plan=self.plan, ends_at=self.now + timedelta(days=days), **{**defaults, **extra}
        )

    def run_at(self, offset_days: float = 0):  # type: ignore[no-untyped-def]
        return send_due_reminders(now=self.now + timedelta(days=offset_days))

    def test_nothing_while_plenty_of_time(self) -> None:
        self.sub(days=10)
        self.assertEqual(self.run_at().reminders, [])
        self.assertEqual(len(mail.outbox), 0)

    def test_seven_day_then_one_day_reminder_each_once(self) -> None:
        sub = self.sub(days=6.5)
        self.assertEqual(self.run_at().reminders, ["asha@acme.in: 7d"])
        self.assertEqual(self.run_at().reminders, [])  # never twice for the same period
        email = mail.outbox[0]
        self.assertEqual(email.subject, "Your Zinwe Startups plan ends in 7 days")
        self.assertIn("https://app.zinwe.test/app/settings/billing", email.body)
        self.assertIn("You can pay early", email.body)
        self.assertIn("Renew my plan", email.alternatives[0][0])

        self.assertEqual(self.run_at(offset_days=5.8).reminders, ["asha@acme.in: 1d"])
        self.assertEqual(mail.outbox[1].subject, "Your Zinwe Startups plan ends tomorrow")
        self.assertEqual(
            sorted(RenewalReminder.objects.filter(subscription=sub).values_list("kind", flat=True)), ["1d", "7d"]
        )

    def test_first_seen_close_to_the_end_gets_only_the_nearest(self) -> None:
        self.sub(days=0.5)
        self.assertEqual(self.run_at().reminders, ["asha@acme.in: 1d"])
        self.assertEqual(len(mail.outbox), 1)

    def test_renewal_under_review_is_not_nagged(self) -> None:
        sub = self.sub(days=5)
        ManualPayment.objects.create(subscription=sub, method="upi", amount_inr=Decimal("999"), reference="R",
                                     paid_on=self.now.date(), payer_name="Asha", is_renewal=True)
        result = self.run_at()
        self.assertEqual((result.reminders, result.skipped_under_review), ([], 1))

    def test_a_renewal_starts_a_fresh_period(self) -> None:
        sub = self.sub(days=5)
        self.run_at()
        sub.ends_at = add_months(sub.ends_at, 1)  # renewed
        sub.save()
        self.assertEqual(self.run_at().reminders, [])  # 35 days out
        self.assertEqual(self.run_at(offset_days=29).reminders, ["asha@acme.in: 7d"])  # next period's 7-day

    def test_free_plans_and_trials_are_ignored(self) -> None:
        self.sub(days=3, amount_inr=Decimal("0"))
        self.sub(days=3, is_trial=True)
        self.assertEqual(self.run_at().reminders, [])

    def test_lapsed_plan_is_expired_and_notified_once(self) -> None:
        sub = self.sub(days=-0.2)
        result = self.run_at()
        sub.refresh_from_db()
        self.assertEqual(sub.status, "expired")
        self.assertEqual(result.expired, ["asha@acme.in"])
        self.assertEqual(mail.outbox[-1].subject, "Your Zinwe Startups plan has expired")
        self.assertIn("Free forever", mail.outbox[-1].body)
        self.assertEqual(self.run_at().expired, [])

    def test_no_expiry_notice_if_they_already_moved_on(self) -> None:
        self.sub(days=-1, status="expired")
        Subscription.objects.create(owner=self.owner, plan=Plan.objects.get(code=Plan.FREE), status="active",
                                    created_at=timezone.now() + timedelta(seconds=1))
        self.assertEqual(self.run_at().expired, [])

    def test_old_expiries_are_not_dug_up(self) -> None:
        self.sub(days=-20, status="expired")
        self.assertEqual(self.run_at().expired, [])

    def test_failed_send_is_retried_next_run(self) -> None:
        self.sub(days=5)
        with mock.patch("z_notification.channels.email.EmailChannel.send", side_effect=OSError("SMTP down")):
            result = self.run_at()
        self.assertEqual((result.reminders, result.failed), ([], 1))
        self.assertFalse(RenewalReminder.objects.exists())
        self.assertEqual(Notification.objects.get().status, "failed")
        self.assertEqual(self.run_at().reminders, ["asha@acme.in: 7d"])

    def test_dry_run_sends_nothing(self) -> None:
        self.sub(days=5)
        self.sub(days=-0.1)
        result = send_due_reminders(now=self.now, dry_run=True)
        self.assertEqual(result.reminders, ["asha@acme.in: 7d"])
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(RenewalReminder.objects.exists())
        self.assertEqual(Subscription.objects.filter(status="expired").count(), 0)

    def test_commands(self) -> None:
        self.sub(days=5)
        out = StringIO()
        call_command("send_renewal_reminders", "--dry-run", stdout=out)
        self.assertIn("Would send reminder -> asha@acme.in: 7d", out.getvalue())
        self.assertEqual(len(mail.outbox), 0)
        out = StringIO()
        call_command("run_jobs", "--once", stdout=out)
        self.assertIn("renewal reminders: 1 reminder(s)", out.getvalue())
        self.assertEqual(len(mail.outbox), 1)
