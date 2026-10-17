"""The owner's event feed filtered to one team member and/or some event types (feature pages' per-person view)."""
from __future__ import annotations

import uuid

from django.utils import timezone
from rest_framework import status

from .models import Device, DeviceEvent, Member
from .tests import DeviceTestCase

EVENTS_URL = "/api/devices/events/"
METRICS_URL = "/api/devices/metrics/"


class EventFilterTests(DeviceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.enroll()
        self.mine = Device.objects.get()
        self.other_member = Member.objects.create(owner=self.owner, name="Rafi Khan", email="rafi@acme.in")
        self.other = Device.objects.create(
            owner=self.owner, member=self.other_member, hostname="SALES-PC", machine_id="m2", token_hash="t2",
        )
        self.as_owner()

    def event(self, device: Device, type_: str) -> DeviceEvent:
        return DeviceEvent.objects.create(
            device=device, client_id=uuid.uuid4(), type=type_, severity="warning",
            occurred_at=timezone.now(), data={},
        )

    def ids(self, query: str) -> set[int]:
        response = self.client.get(f"{EVENTS_URL}?{query}")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        return {row["id"] for row in response.json()}

    def test_member_filter(self) -> None:
        a = self.event(self.mine, "usb.connected")
        b = self.event(self.other, "usb.connected")
        self.assertEqual(self.ids(""), {a.id, b.id})
        self.assertEqual(self.ids(f"member={self.member.id}"), {a.id})
        self.assertEqual(self.ids(f"member={self.other_member.id}"), {b.id})

    def test_types_filter(self) -> None:
        web = self.event(self.mine, "web.blocked_attempt")
        usb = self.event(self.mine, "usb.connected")
        remote = self.event(self.other, "remote.detected")
        self.assertEqual(self.ids("types=web.blocked_attempt"), {web.id})
        self.assertEqual(self.ids("types=usb.connected, remote.detected"), {usb.id, remote.id})
        self.assertEqual(self.ids(f"types=usb.connected,remote.detected&member={self.member.id}"), {usb.id})

    def test_someone_elses_member_sees_nothing(self) -> None:
        from z_auth.models import User

        stranger = User.objects.create_user("x@else.in", "pw-12345678")
        theirs = Member.objects.create(owner=stranger, name="X", email="x@else.in")
        device = Device.objects.create(owner=stranger, member=theirs, hostname="X", machine_id="x", token_hash="x")
        self.event(device, "usb.connected")
        self.assertEqual(self.ids(f"member={theirs.id}"), set())

    def test_too_many_types_rejected(self) -> None:
        many = ",".join(f"t{i}" for i in range(41))
        self.assertEqual(self.client.get(f"{EVENTS_URL}?types={many}").status_code, status.HTTP_400_BAD_REQUEST)

    def test_metrics_member_filter_still_works(self) -> None:
        self.assertEqual(self.client.get(f"{METRICS_URL}?member={self.other_member.id}").status_code, status.HTTP_200_OK)
