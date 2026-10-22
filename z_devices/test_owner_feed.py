"""FR-15: what's new for the owner's browser notifications, and their notification settings."""
from __future__ import annotations

import uuid
from datetime import timedelta
from urllib.parse import quote

from django.utils import timezone
from rest_framework import status

from .models import Device, DeviceEvent, Message, NotificationPrefs, WorkLog
from .tests import DeviceTestCase

FEED = "/api/devices/notifications/"
PREFS = "/api/devices/notifications/settings/"


class OwnerFeedTests(DeviceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.enroll()
        self.device = Device.objects.get()
        self.as_owner()
        self.since = timezone.now() - timedelta(seconds=1)

    def feed(self, since=...):  # type: ignore[no-untyped-def]
        """`since` defaults to just before the test; None = the first call (no since)."""
        since = self.since if since is ... else since
        url = FEED if since is None else f"{FEED}?since={quote(since.isoformat())}"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        return response.json()

    def reply(self, body: str = "Done, sending the file now") -> Message:
        return Message.objects.create(owner=self.owner, member=self.member, sender=Message.Sender.MEMBER, body=body)

    def alert(self, severity: str = "warning") -> DeviceEvent:
        return DeviceEvent.objects.create(
            device=self.device, client_id=uuid.uuid4(), type="usb.connected", severity=severity,
            occurred_at=timezone.now(), data={},
        )

    def break_request(self) -> WorkLog:
        return WorkLog.objects.create(
            owner=self.owner, device=self.device, member=self.member, state=WorkLog.State.ON_BREAK,
            at=timezone.now(), reason="Lunch", approval=WorkLog.Approval.PENDING,
        )

    def test_first_call_has_no_backlog(self) -> None:
        self.reply()
        body = self.feed(since=None)
        self.assertEqual(body["items"], [])
        self.assertTrue(body["now"])

    def test_new_reply_alert_and_break_request(self) -> None:
        self.reply()
        self.alert("critical")
        self.break_request()
        items = {i["kind"]: i for i in self.feed()["items"]}
        self.assertEqual(set(items), {"chat", "alert", "approval"})
        self.assertEqual(items["chat"]["title"], "Asha Rao wrote")
        self.assertEqual(items["chat"]["link"], f"/app/chat?member={self.member.id}")
        self.assertEqual(items["alert"]["title"], "Critical alert")
        self.assertIn("ACCOUNTS-PC-01", items["alert"]["body"])
        self.assertEqual(items["approval"]["title"], "Asha Rao wants to take a break")

    def test_only_what_is_new(self) -> None:
        self.reply()
        later = self.feed()["now"]
        from django.utils.dateparse import parse_datetime

        self.assertEqual(self.feed(since=parse_datetime(later))["items"], [])

    def test_never_more_than_an_hour_back(self) -> None:
        old = self.reply()
        Message.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(hours=3))
        self.assertEqual(self.feed(since=timezone.now() - timedelta(days=2))["items"], [])

    def test_owner_lines_and_confirmations_are_not_news(self) -> None:
        Message.objects.create(owner=self.owner, member=self.member, sender=Message.Sender.OWNER, body="Hi")
        DeviceEvent.objects.create(
            device=self.device, client_id=uuid.uuid4(), type="usb.policy_applied", severity="warning",
            occurred_at=timezone.now(), data={},
        )
        self.assertEqual(self.feed()["items"], [])

    def test_settings_choose_what_shows(self) -> None:
        self.assertEqual(self.client.get(PREFS).json(), {"chat": True, "alerts": "warning", "approvals": True})
        self.alert("info")
        self.assertEqual(self.feed()["items"], [])  # default: warnings and up
        response = self.client.patch(PREFS, {"chat": False, "alerts": "all"}, format="json")
        self.assertEqual(response.json(), {"chat": False, "alerts": "all", "approvals": True})
        self.reply()
        self.assertEqual([i["kind"] for i in self.feed()["items"]], ["alert"])
        self.assertEqual(self.client.patch(PREFS, {"alerts": "loud"}, format="json").status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(NotificationPrefs.objects.count(), 1)

    def test_other_owners_never_leak(self) -> None:
        from z_auth.models import User

        from .models import Member

        other = User.objects.create_user("other@b.in", "pw-12345678")
        member = Member.objects.create(owner=other, name="Ravi", email="ravi@b.in")
        Message.objects.create(owner=other, member=member, sender=Message.Sender.MEMBER, body="psst")
        self.assertEqual(self.feed()["items"], [])

    def test_bad_since_and_login(self) -> None:
        self.assertEqual(self.client.get(f"{FEED}?since=yesterday").status_code, status.HTTP_400_BAD_REQUEST)
        self.client.credentials()
        self.assertEqual(self.client.get(FEED).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.client.get(PREFS).status_code, status.HTTP_401_UNAUTHORIZED)
