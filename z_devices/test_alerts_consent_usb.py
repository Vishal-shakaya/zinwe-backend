"""
Fix round 2026-10-17 (user-journey test): handled alerts, rule confirmations, USB lockdown + Restore USB,
declined consent, a new Member ID emailed, and the owner told about critical alerts / a full plan.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.core import mail
from django.utils import timezone
from rest_framework import status

from z_auth.models import Plan, Subscription, User
from z_notification.models import Notification

from .models import Device, DeviceEvent, Member, Message, UsageMetric
from .tests import (
    CURRENT_POLICY_URL,
    EVENTS_URL,
    HEARTBEAT_URL,
    LIST_URL,
    MEMBERS_URL,
    METRICS_UPLOAD_URL,
    UPLOAD_URL,
    DeviceTestCase,
    make_event,
)

COUNTS_URL = "/api/devices/events/counts/"


def handled_url(event_id: int) -> str:
    return f"/api/devices/events/{event_id}/handled/"


def restore_url(device_id: int) -> str:
    return f"/api/devices/{device_id}/usb/restore/"


class EnrolledTestCase(DeviceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.owner.full_name = "Vikram Mehta"
        self.owner.save(update_fields=["full_name"])
        self.token = self.enroll().json()["device_token"]
        self.device = Device.objects.get()

    def upload(self, *events: dict[str, Any]):  # type: ignore[no-untyped-def]
        self.as_device(self.token)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(UPLOAD_URL, {"events": list(events)}, format="json")

    def beat(self, **data: Any) -> dict[str, Any]:
        self.as_device(self.token)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(HEARTBEAT_URL, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        return response.json()

    def owner_post(self, url: str, data: Any = None):  # type: ignore[no-untyped-def]
        self.as_owner()
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(url, data or {}, format="json")

    def event(self, type: str = "usb.connected", severity: str = "info", **data: Any) -> DeviceEvent:
        self.upload(make_event(type=type, severity=severity, data=data or {"device_name": "SanDisk"}))
        return DeviceEvent.objects.filter(type=type).order_by("-id").first()


# --- Handled alerts (J5-04) ------------------------------------------------------------------


class HandledAlertTests(EnrolledTestCase):
    def test_mark_handled_and_unhandled(self) -> None:
        event = self.event()
        response = self.owner_post(handled_url(event.id), {"handled": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(body["id"], event.id)
        self.assertIsNotNone(body["handled_at"])
        self.assertEqual(body["handled_by"], "Vikram Mehta")
        self.assertIn("emailed_at", body)

        again = self.owner_post(handled_url(event.id), {}).json()  # default handled=true; first time is kept
        self.assertEqual(again["handled_at"], body["handled_at"])

        undone = self.owner_post(handled_url(event.id), {"handled": False}).json()
        self.assertIsNone(undone["handled_at"])
        self.assertEqual(undone["handled_by"], "")

    def test_feed_shows_handled_and_filters(self) -> None:
        first, second = self.event(), self.event("usb.blocked", "warning")
        self.owner_post(handled_url(first.id))
        self.as_owner()
        rows = {r["id"]: r for r in self.client.get(EVENTS_URL).json()}
        self.assertIsNotNone(rows[first.id]["handled_at"])
        self.assertIsNone(rows[second.id]["handled_at"])
        self.assertEqual(rows[second.id]["handled_by"], "")
        open_ids = [r["id"] for r in self.client.get(EVENTS_URL, {"handled": "false"}).json()]
        self.assertEqual(open_ids, [second.id])

    def test_validation_auth_and_other_owners(self) -> None:
        event = self.event()
        bad = self.owner_post(handled_url(event.id), {"handled": "maybe"})
        self.assertEqual(bad.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("handled", bad.json())

        self.client.credentials()
        self.assertEqual(self.client.post(handled_url(event.id), {}).status_code, status.HTTP_401_UNAUTHORIZED)
        self.as_device(self.token)
        self.assertEqual(self.client.post(handled_url(event.id), {}).status_code, status.HTTP_401_UNAUTHORIZED)

        other = User.objects.create_user("other@else.in", "pw-12345678")
        self.client.force_authenticate(other)
        self.assertEqual(self.client.post(handled_url(event.id), {}).status_code, status.HTTP_404_NOT_FOUND)

    def test_message_from_an_alert_marks_it_handled(self) -> None:
        event = self.event("usb.blocked", "warning")
        response = self.owner_post(
            f"{MEMBERS_URL}{self.member.id}/message/",
            {"title": "Please explain", "event_id": event.id},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        event.refresh_from_db()
        self.assertIsNotNone(event.handled_at)
        self.assertEqual(event.handled_by, "Vikram Mehta")
        self.assertEqual(Message.objects.get().title, "Please explain")

    def test_message_with_someone_elses_event_is_refused(self) -> None:
        other = User.objects.create_user("other@else.in", "pw-12345678")
        other_device = Device.objects.create(owner=other, machine_id="x", hostname="X", token_hash="z" * 64)
        foreign = DeviceEvent.objects.create(
            device=other_device, client_id="11111111-1111-4111-8111-111111111111", type="usb.connected",
            occurred_at=timezone.now(),
        )
        response = self.owner_post(
            f"{MEMBERS_URL}{self.member.id}/message/", {"title": "Hi", "event_id": foreign.id}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("event_id", response.json())
        self.assertFalse(Message.objects.exists())
        foreign.refresh_from_db()
        self.assertIsNone(foreign.handled_at)


# --- Rule confirmations are not alerts (J5-01) ------------------------------------------------


class EventKindTests(EnrolledTestCase):
    def test_kind_and_kind_filter_and_counts(self) -> None:
        alert = self.event("usb.blocked", "warning")
        applied = self.event("web.blocklist_applied")
        usb_rule = self.event("usb.policy_applied", mode="block")
        locked = self.event("data.usb_locked_down", "critical")
        self.as_owner()
        rows = {r["id"]: r for r in self.client.get(EVENTS_URL).json()}
        self.assertEqual(rows[alert.id]["kind"], "alert")
        self.assertEqual(rows[locked.id]["kind"], "alert")
        self.assertEqual(rows[applied.id]["kind"], "confirmation")
        self.assertEqual(rows[usb_rule.id]["kind"], "confirmation")
        # Plain words, not "Data usb locked down".
        self.assertEqual(rows[locked.id]["title"], "USB switched to read-only on ACCOUNTS-PC-01 after a large copy")

        alerts = {r["id"] for r in self.client.get(EVENTS_URL, {"kind": "alert"}).json()}
        self.assertEqual(alerts, {alert.id, locked.id})
        confirmations = {r["id"] for r in self.client.get(EVENTS_URL, {"kind": "confirmation"}).json()}
        self.assertEqual(confirmations, {applied.id, usb_rule.id})
        self.assertEqual(self.client.get(EVENTS_URL, {"kind": "nope"}).status_code, status.HTTP_400_BAD_REQUEST)

        self.owner_post(handled_url(alert.id))
        self.as_owner()
        counts = self.client.get(COUNTS_URL).json()
        self.assertEqual(counts, {"alerts": 2, "unhandled": 1, "critical_unhandled": 1})

    def test_counts_need_an_owner(self) -> None:
        self.assertEqual(self.client.get(COUNTS_URL).status_code, status.HTTP_401_UNAUTHORIZED)


# --- USB lockdown + Restore USB (J5-02) --------------------------------------------------------


class UsbRestoreTests(EnrolledTestCase):
    def test_lockdown_restore_and_heartbeat_flag(self) -> None:
        self.assertFalse(self.beat()["usb_restore"])
        self.upload(make_event(type="data.usb_locked_down", severity="critical", occurred_at=timezone.now().isoformat()))
        self.device.refresh_from_db()
        self.assertIsNotNone(self.device.usb_locked_at)
        self.as_owner()
        row = self.client.get(LIST_URL).json()[0]
        self.assertIsNotNone(row["usb_locked_at"])
        self.assertIsNone(row["usb_restore_requested_at"])
        member_device = self.client.get(MEMBERS_URL).json()[0]["device"]
        self.assertEqual(member_device["id"], self.device.id)
        self.assertIsNotNone(member_device["usb_locked_at"])

        response = self.owner_post(restore_url(self.device.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNotNone(response.json()["usb_restore_requested_at"])
        self.assertEqual(response.json()["id"], self.device.id)

        self.assertTrue(self.beat()["usb_restore"])  # told once...
        self.assertFalse(self.beat()["usb_restore"])  # ...not on every heartbeat

        self.upload(make_event(type="usb.restored", occurred_at=timezone.now().isoformat()))
        self.device.refresh_from_db()
        self.assertIsNone(self.device.usb_locked_at)
        self.assertIsNone(self.device.usb_restore_requested_at)
        # Pressing it again later is told again.
        self.owner_post(restore_url(self.device.id))
        self.assertTrue(self.beat()["usb_restore"])

    def test_restore_auth_and_ownership(self) -> None:
        self.client.credentials()
        self.assertEqual(self.client.post(restore_url(self.device.id)).status_code, status.HTTP_401_UNAUTHORIZED)
        self.as_device(self.token)
        self.assertEqual(self.client.post(restore_url(self.device.id)).status_code, status.HTTP_401_UNAUTHORIZED)
        other = User.objects.create_user("other@else.in", "pw-12345678")
        self.client.force_authenticate(other)
        self.assertEqual(self.client.post(restore_url(self.device.id)).status_code, status.HTTP_404_NOT_FOUND)
        self.client.force_authenticate(None)
        self.assertEqual(self.owner_post(restore_url(99999)).status_code, status.HTTP_404_NOT_FOUND)

    def test_removed_pc_cannot_be_restored(self) -> None:
        Device.objects.filter(pk=self.device.pk).update(is_revoked=True)
        self.assertEqual(self.owner_post(restore_url(self.device.id)).status_code, status.HTTP_409_CONFLICT)


# --- Declined consent (J6-03) -----------------------------------------------------------------


class DeclinedConsentTests(EnrolledTestCase):
    def policy(self) -> dict[str, Any]:
        self.as_device(self.token)
        return self.client.get(CURRENT_POLICY_URL).json()

    def test_pending_then_declined_then_given(self) -> None:
        before = self.beat()
        self.assertEqual(before["consent_state"], "pending")
        self.assertTrue(self.policy()["monitoring_enabled"])

        declined = self.beat(consent={"state": "declined", "version": "2026-10-17"})
        self.assertEqual(declined["consent_state"], "declined")
        self.assertNotEqual(declined["policy_version"], before["policy_version"])  # the agent re-fetches
        policy = self.policy()
        self.assertEqual(policy["version"], declined["policy_version"])
        self.assertFalse(policy["monitoring_enabled"])
        self.assertEqual(policy["consent_state"], "declined")
        self.assertEqual(policy["usb"]["mode"], "allow")
        self.assertFalse(policy["web"]["enabled"])
        self.assertFalse(policy["data_transfer"]["enabled"])
        self.assertFalse(policy["data_transfer"]["usb_lockdown"])
        self.assertFalse(policy["remote_software"]["enabled"])
        self.assertFalse(policy["remote_software"]["blocking_enabled"])
        self.assertEqual(policy["entitlements"], ["team_chat", "work_time"])  # work hours + chat still work

        self.as_owner()
        row = self.client.get(LIST_URL).json()[0]
        self.assertEqual(row["consent_state"], "declined")
        self.assertIsNotNone(row["consent_declined_at"])
        self.assertEqual(self.client.get(MEMBERS_URL).json()[0]["device"]["consent_state"], "declined")

        given = self.beat(consent={"state": "given", "version": "2026-10-17"})
        self.assertEqual(given["consent_state"], "given")
        self.assertTrue(self.policy()["monitoring_enabled"])
        self.device.refresh_from_db()
        self.assertIsNone(self.device.consent_declined_at)
        self.assertEqual(self.device.consent_version, "2026-10-17")

    def test_declined_pc_uploads_are_dropped_but_work_hours_and_chat_work(self) -> None:
        self.beat(consent={"state": "declined"})
        response = self.upload(make_event(type="usb.connected"), make_event(type="web.blocked_attempt"))
        self.assertEqual(response.json(), {"accepted": 2, "duplicates": 0})  # handled, so the agent clears them
        self.assertFalse(DeviceEvent.objects.exists())
        self.as_device(self.token)
        metrics = self.client.post(
            METRICS_UPLOAD_URL,
            {"metrics": [{"bucket_start": timezone.now().isoformat(), "category": "app.foreground", "key": "x", "seconds": 5}]},
            format="json",
        )
        self.assertEqual(metrics.json(), {"accepted": 1})
        self.assertFalse(UsageMetric.objects.exists())

        self.beat(consent={"state": "declined"}, work_state="working", worked_seconds=60)
        self.device.refresh_from_db()
        self.assertEqual(self.device.work_state, "working")
        self.as_device(self.token)
        reply = self.client.post("/api/devices/messages/", {"body": "Can we talk about this?"}, format="json")
        self.assertEqual(reply.status_code, status.HTTP_201_CREATED)

    def test_old_agent_consent_fields_still_mean_given(self) -> None:
        body = self.beat(consent_version="2026-10-11", consent_at=timezone.now().isoformat())
        self.assertEqual(body["consent_state"], "given")

    def test_bad_consent_state_is_refused(self) -> None:
        self.as_device(self.token)
        response = self.client.post(HEARTBEAT_URL, {"consent": {"state": "maybe"}}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


# --- New Member ID emailed (J5-05) ------------------------------------------------------------


class RotateEmailTests(DeviceTestCase):
    def test_rotate_emails_the_new_id_by_default(self) -> None:
        self.owner.company = "Kulkarni & Associates"
        self.owner.save(update_fields=["company"])
        old = self.member.member_code
        self.as_owner()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"{MEMBERS_URL}{self.member.id}/rotate/", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        new = response.json()["member_id"]
        self.assertNotEqual(new, old)
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ["asha@acme.in"])
        self.assertEqual(sent.subject, "Your new Zinwe Member ID from Kulkarni & Associates")
        self.assertIn(new, sent.body)
        self.assertIn("the old one no longer works", sent.body)
        self.assertNotIn(new, Notification.objects.get().body)  # masked in our log

    def test_rotate_without_email_and_old_url(self) -> None:
        self.as_owner()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"{MEMBERS_URL}{self.member.id}/rotate-id/", {"email": False}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 0)

    def test_rotate_validation_and_auth(self) -> None:
        self.as_owner()
        bad = self.client.post(f"{MEMBERS_URL}{self.member.id}/rotate/", {"email": "perhaps"}, format="json")
        self.assertEqual(bad.status_code, status.HTTP_400_BAD_REQUEST)
        self.client.credentials()
        self.assertEqual(
            self.client.post(f"{MEMBERS_URL}{self.member.id}/rotate/").status_code, status.HTTP_401_UNAUTHORIZED
        )


# --- The owner hears about critical alerts (J5-21) and a full plan (J3-19) ---------------------


class OwnerNoticeTests(EnrolledTestCase):
    def test_critical_alert_emails_the_owner_throttled(self) -> None:
        self.member.name = "Priya"
        self.member.save(update_fields=["name"])
        self.upload(make_event(type="usb.connected"))  # not critical: no email
        self.assertEqual(len(mail.outbox), 0)
        self.upload(make_event(type="data.usb_locked_down", severity="critical"))
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ["owner@acme.in"])
        self.assertIn("USB switched to read-only", sent.subject)
        self.assertIn("Priya", sent.body)
        self.assertIn("/app/alerts", sent.body)
        self.upload(make_event(type="data.usb_locked_down", severity="critical"))
        self.assertEqual(len(mail.outbox), 1)  # a second one within 30 minutes: no new email
        Notification.objects.update(created_at=timezone.now() - timedelta(hours=1))
        self.upload(make_event(type="data.usb_locked_down", severity="critical"))
        self.assertEqual(len(mail.outbox), 2)

    def test_critical_confirmation_does_not_email(self) -> None:
        self.upload(make_event(type="usb.policy_applied", severity="critical"))
        self.assertEqual(len(mail.outbox), 0)


class PcLimitNoticeTests(DeviceTestCase):
    def test_owner_is_told_once_when_a_pc_is_refused(self) -> None:
        Subscription.objects.filter(owner=self.owner).delete()  # Free forever: 3 PCs
        self.assertEqual(Plan.objects.get(code="free").max_devices, 3)
        for n in range(3):
            self.assertEqual(self.enroll(machine_id=f"pc-{n}").status_code, status.HTTP_201_CREATED)
        mail.outbox.clear()
        refused = self.enroll(machine_id="pc-4", hostname="STAFF-11")
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ["owner@acme.in"])
        self.assertEqual(sent.subject, "Asha Rao couldn't connect a PC: your plan is full")
        self.assertIn("STAFF-11", sent.body)
        self.assertIn("/onboarding/plan?plan=startups", sent.body)
        self.assertIn("upgrade to Startups (up to 10 PCs)", sent.body)
        self.enroll(machine_id="pc-5")
        self.assertEqual(len(mail.outbox), 1)  # rate-limited
        self.assertEqual(Member.objects.count(), 1)


class EmployerNamesTests(DeviceTestCase):
    """The agent's consent notice names the employer (company + owner), from the heartbeat and the policy."""

    def test_heartbeat_and_policy_name_the_company(self) -> None:
        from z_auth.models import Company

        Company.objects.create(owner=self.owner, name="Kulkarni & Associates", industry="other",
                               team_size="6-20", phone="9876543210", city="Pune", state="Maharashtra")
        self.owner.full_name = "Rakesh Kulkarni"
        self.owner.save()
        token = self.enroll().json()["device_token"]
        self.as_device(token)
        beat = self.client.post(HEARTBEAT_URL, {}, format="json").json()
        self.assertEqual((beat["company_name"], beat["owner_name"]), ("Kulkarni & Associates", "Rakesh Kulkarni"))
        policy = self.client.get(CURRENT_POLICY_URL).json()
        self.assertEqual(policy["company_name"], "Kulkarni & Associates")
