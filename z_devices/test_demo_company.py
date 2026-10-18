"""The demo account (manage.py seed_demo_company) and the demo-domain email/report guards (z_main/demo.py)."""
from __future__ import annotations

from io import StringIO

from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from z_adminapi.services import dashboard_metrics
from z_auth.billing import current_subscription
from z_auth.models import Plan, User
from z_devices.management.commands.seed_demo_company import OWNER_EMAIL, PEOPLE
from z_devices.models import Device, DeviceEvent, Member, Message, UsageMetric, WorkLog
from z_main.demo import is_demo_email
from z_notification.models import Notification
from z_notification.services import notify


def seed(*args: str) -> str:
    out = StringIO()
    call_command("seed_demo_company", "--force", *args, stdout=out)
    return out.getvalue()


class DemoCompanyTests(TestCase):
    def setUp(self) -> None:
        if not Plan.objects.filter(code="businesses").exists():
            Plan.objects.create(code="businesses", name="Businesses", max_devices=50, history_days=365,
                                price_inr_monthly=2834, modules=["web", "remote_access", "team_chat", "work_time"])

    def test_builds_a_full_account(self) -> None:
        output = seed("--password", "Demo-pass-2026!")
        owner = User.objects.get(email=OWNER_EMAIL)
        self.assertTrue(owner.check_password("Demo-pass-2026!"))
        self.assertIn("Demo-pass-2026!", output)
        subscription = current_subscription(owner)
        self.assertEqual(subscription.plan.code, "businesses")
        self.assertEqual(subscription.status, "active")
        self.assertEqual(Member.objects.filter(owner=owner).count(), len(PEOPLE))
        self.assertEqual(Device.objects.filter(owner=owner).count(), len(PEOPLE))
        self.assertTrue(WorkLog.objects.filter(owner=owner).exists())
        self.assertTrue(DeviceEvent.objects.filter(device__owner=owner, severity="warning", handled_at__isnull=True).exists())
        self.assertTrue(UsageMetric.objects.filter(device__owner=owner, category="app.foreground").exists())
        self.assertTrue(Message.objects.filter(owner=owner, sender="member", read_by_owner=False).exists())
        # Mid-afternoon "now": people online, one break waiting, one on leave.
        self.assertTrue(any(d.is_online for d in Device.objects.filter(owner=owner)))
        self.assertTrue(WorkLog.objects.filter(owner=owner, approval="pending").exists())
        self.assertEqual(Device.objects.filter(owner=owner, work_state="not_started").count(), 1)
        self.assertEqual(len(mail.outbox), 0)

    def test_rerun_keeps_login_and_member_ids(self) -> None:
        seed("--password", "Demo-pass-2026!")
        codes = set(Member.objects.values_list("member_code", flat=True))
        logs = WorkLog.objects.count()
        output = seed()
        self.assertIn("password: unchanged", output)
        self.assertTrue(User.objects.get(email=OWNER_EMAIL).check_password("Demo-pass-2026!"))
        self.assertEqual(set(Member.objects.values_list("member_code", flat=True)), codes)
        self.assertEqual(WorkLog.objects.count(), logs)  # rebuilt, not doubled

    def test_clear_removes_everything(self) -> None:
        seed()
        seed("--clear")
        self.assertFalse(User.objects.filter(email=OWNER_EMAIL).exists())
        self.assertFalse(Device.objects.filter(machine_id__startswith="demo-kc-").exists())

    def test_refuses_without_force_when_debug_is_off(self) -> None:
        with self.assertRaises(CommandError):
            call_command("seed_demo_company", stdout=StringIO())

    @override_settings(ZINWE_DEMO_EMAIL_DOMAINS=[])
    def test_refuses_when_the_domain_is_not_a_demo_domain(self) -> None:
        with self.assertRaises(CommandError):
            seed()


class DemoEmailTests(TestCase):
    def test_demo_addresses_are_logged_not_delivered(self) -> None:
        self.assertTrue(is_demo_email("Someone@KitecrestDigital.com"))
        self.assertFalse(is_demo_email("owner@acme.in"))
        note = notify("email", "arjun.mehta@kitecrestdigital.com", "member_message",
                      {"owner_name": "A", "company_name": "K", "title": "Hi", "body": "Hello", "member_name": "Arjun"})
        self.assertEqual(note.status, Notification.Status.SENT)
        self.assertIn("demo address", note.error)
        self.assertEqual(len(mail.outbox), 0)

    def test_real_addresses_still_go_out(self) -> None:
        notify("email", "owner@acme.in", "member_message",
               {"owner_name": "A", "company_name": "K", "title": "Hi", "body": "Hello", "member_name": "B"})
        self.assertEqual(len(mail.outbox), 1)

    def test_admin_dashboard_leaves_demo_accounts_out(self) -> None:
        User.objects.create_user("owner@acme.in", "x-long-password-1")
        User.objects.create_user("ananya.rao@kitecrestdigital.com", "x-long-password-1")
        self.assertEqual(dashboard_metrics()["totals"]["users"], 1)
