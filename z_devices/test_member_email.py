"""Emailing members: chat notes with also_email, and one-click alert emails from Activity."""
from __future__ import annotations

import uuid
from typing import Any

from django.core import mail
from django.utils import timezone
from rest_framework import status

from .models import Device, DeviceEvent, Member
from .tests import DeviceTestCase

MEMBERS_URL = "/api/devices/members/"
EVENTS_URL = "/api/devices/events/"


class MemberEmailTests(DeviceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.enroll()
        self.device = Device.objects.get()
        mail.outbox.clear()
        self.as_owner()

    def event(self, device: Device | None = None, **fields: Any) -> DeviceEvent:
        return DeviceEvent.objects.create(
            device=device or self.device, client_id=uuid.uuid4(), type=fields.pop("type", "usb.blocked"),
            severity="warning", occurred_at=timezone.now(), data={"device_name": "SanDisk"}, **fields,
        )

    def post(self, url: str, body: dict[str, Any]):  # type: ignore[no-untyped-def]
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(url, body, format="json")

    # --- chat note, also by email -------------------------------------------

    def test_message_also_emailed_when_asked(self) -> None:
        response = self.post(
            f"{MEMBERS_URL}{self.member.id}/message/",
            {"title": "Please lock your PC", "body": "Before lunch, thanks.", "severity": "critical", "also_email": True},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertEqual(email.to, ["asha@acme.in"])
        self.assertTrue(email.subject.startswith("Urgent: Please lock your PC"))
        self.assertIn("Before lunch, thanks.", email.body)

    def test_message_not_emailed_by_default(self) -> None:
        self.post(f"{MEMBERS_URL}{self.member.id}/message/", {"title": "Hi"})
        self.assertEqual(len(mail.outbox), 0)

    # --- alert email ----------------------------------------------------------

    def test_alert_email_once_and_shown_in_feed(self) -> None:
        event = self.event()
        url = f"{EVENTS_URL}{event.id}/email/"
        body = {"label": "USB drive blocked", "suggested": "USB storage is blocked on your work PC."}
        response = self.post(url, body)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertEqual(response.json()["sent_to"], "asha@acme.in")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("USB drive blocked", mail.outbox[0].subject)
        self.assertIn("USB storage is blocked on your work PC.", mail.outbox[0].body)
        self.assertIn(self.device.hostname, mail.outbox[0].body)

        again = self.post(url, body)
        self.assertEqual(again.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(len(mail.outbox), 1)

        feed = self.client.get(EVENTS_URL).json()
        self.assertIsNotNone(next(row for row in feed if row["id"] == event.id)["emailed_at"])

    def test_pc_without_member(self) -> None:
        Device.objects.filter(pk=self.device.pk).update(member=None)
        response = self.post(f"{EVENTS_URL}{self.event().id}/email/", {})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(len(mail.outbox), 0)

    def test_someone_elses_event_is_404(self) -> None:
        from z_auth.models import User

        other = User.objects.create_user("other@else.in", "pw-12345678")
        theirs = Member.objects.create(owner=other, name="Rafi", email="rafi@else.in")
        device = Device.objects.create(
            owner=other, member=theirs, hostname="THEIRS", machine_id="x", token_hash="y",
        )
        response = self.post(f"{EVENTS_URL}{self.event(device).id}/email/", {})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(len(mail.outbox), 0)
