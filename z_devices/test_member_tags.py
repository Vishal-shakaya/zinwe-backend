"""FR-19: a member's role (their card's theme) and the playful, kind tags from their week."""
from __future__ import annotations

from datetime import datetime, timedelta

from django.utils import timezone
from rest_framework import status

from .models import Device, Member, Message, UsageMetric, WorkLog
from .tags import tags_for_owner
from .tests import MEMBERS_URL, DeviceTestCase


class MemberRoleTests(DeviceTestCase):
    def test_role_on_create_and_change(self) -> None:
        self.as_owner()
        created = self.client.post(MEMBERS_URL, {"name": "Ravi", "email": "ravi@acme.in", "role": "sales"}, format="json")
        self.assertEqual(created.json()["role"], "sales")
        url = f"{MEMBERS_URL}{created.json()['id']}/"
        self.assertEqual(self.client.patch(url, {"role": "developer"}, format="json").json()["role"], "developer")
        self.assertEqual(self.client.patch(url, {"role": ""}, format="json").json()["role"], "")
        self.assertEqual(self.client.patch(url, {"role": "boss"}, format="json").status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.client.get(MEMBERS_URL).json()[0]["role"], "")

    def test_only_your_own_team(self) -> None:
        from z_auth.models import User

        other = User.objects.create_user("other@b.in", "pw-12345678")
        theirs = Member.objects.create(owner=other, name="X", email="x@b.in")
        self.as_owner()
        response = self.client.patch(f"{MEMBERS_URL}{theirs.id}/", {"role": "sales"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class MemberTagTests(DeviceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.enroll()
        self.device = Device.objects.get()
        Member.objects.filter(pk=self.member.pk).update(created_at=timezone.now() - timedelta(days=30))
        self.today = timezone.localdate()

    def at(self, days_ago: int, hour: int, minute: int = 0) -> datetime:
        day = self.today - timedelta(days=days_ago)
        return timezone.make_aware(datetime.combine(day, datetime.min.time()) + timedelta(hours=hour, minutes=minute))

    def day(self, days_ago: int, start: int = 9, end: int = 18) -> None:
        log = lambda state, when: WorkLog.objects.create(  # noqa: E731
            owner=self.owner, device=self.device, member=self.member, state=state, at=when)
        log("working", self.at(days_ago, start))
        log("on_break", self.at(days_ago, 13))
        log("working", self.at(days_ago, 13, 40))
        log("ended", self.at(days_ago, end))

    def keys(self) -> list[str]:
        return [t["key"] for t in tags_for_owner(self.owner)[str(self.member.pk)]]

    def test_a_steady_early_week(self) -> None:
        for d in range(1, 6):
            self.day(d, start=8)
        self.assertEqual(self.keys(), ["streak", "early_bird", "balanced"])  # three at most, in order

    def test_focus_talk_and_late_finishes(self) -> None:
        for d in (1, 2):
            self.day(d, start=10, end=21)
        UsageMetric.objects.create(device=self.device, bucket_start=self.at(1, 10), category="app.foreground",
                                   key="code", seconds=3 * 3600, count=1)
        UsageMetric.objects.create(device=self.device, bucket_start=self.at(1, 11), category="app.foreground",
                                   key="chrome", seconds=1800, count=1)
        for _ in range(5):
            Message.objects.create(owner=self.owner, member=self.member, sender=Message.Sender.MEMBER, body="ok")
        self.assertEqual(self.keys(), ["deep_focus", "team_talker", "night_owl"])

    def test_new_and_quiet_members(self) -> None:
        self.assertEqual(self.keys(), [])
        Member.objects.filter(pk=self.member.pk).update(created_at=timezone.now())
        self.assertEqual(self.keys(), ["new_here"])

    def test_endpoint_is_owner_only(self) -> None:
        self.as_owner()
        body = self.client.get(f"{MEMBERS_URL}tags/").json()
        self.assertEqual(body, {str(self.member.pk): []})
        self.client.credentials()
        self.assertEqual(self.client.get(f"{MEMBERS_URL}tags/").status_code, status.HTTP_401_UNAUTHORIZED)
