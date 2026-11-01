"""
The Chat page's tiles: GET /api/devices/members/ carries each person's latest line and unread count, in the same
number of queries however many people there are, and the text stays behind the same lock as the thread.
"""
from __future__ import annotations

from datetime import timedelta

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status

from z_auth._testing import give_plan
from z_auth.models import Plan, Subscription, User

from .models import Device, Member, Message
from .services import TILE_TEXT_MAX
from .tests import MEMBERS_URL, MESSAGES_URL, DeviceTestCase


class ChatTileTests(DeviceTestCase):
    def say(self, body: str = "", member: Member | None = None, **fields) -> Message:  # type: ignore[no-untyped-def]
        sender = fields.pop("sender", Message.Sender.MEMBER)
        return Message.objects.create(
            owner=self.owner, member=member or self.member, sender=sender, body=body,
            read_by_owner=sender == Message.Sender.OWNER, **fields,
        )

    def row(self) -> dict:
        self.as_owner()
        return next(m for m in self.client.get(MEMBERS_URL).json() if m["id"] == self.member.id)

    def test_no_messages_yet(self) -> None:
        row = self.row()
        self.assertIsNone(row["last_message"])
        self.assertEqual(row["unread"], 0)

    def test_the_latest_line_with_time_sender_and_count(self) -> None:
        self.say("Can you check the banner?")
        latest = self.say("The second one is my pick.")
        row = self.row()
        self.assertEqual(row["unread"], 2)
        self.assertEqual(row["last_message"]["text"], "The second one is my pick.")
        self.assertEqual(row["last_message"]["sender"], "member")
        self.assertEqual(row["last_message"]["at"], latest.created_at.isoformat().replace("+00:00", "Z"))
        self.assertEqual(set(row["last_message"]), {"text", "sender", "at"})

    def test_latest_goes_by_time_not_by_id(self) -> None:
        newer = self.say("newer")
        older = self.say("older")
        Message.objects.filter(pk=older.pk).update(created_at=newer.created_at - timedelta(minutes=5))
        self.assertEqual(self.row()["last_message"]["text"], "newer")

    def test_your_own_line_is_the_latest_and_the_count_stays(self) -> None:
        self.say("Are you free?")
        self.say("Yes, calling you now", sender=Message.Sender.OWNER, title="Re: Are you free?")
        row = self.row()
        self.assertEqual(row["last_message"]["sender"], "owner")
        self.assertEqual(row["last_message"]["text"], "Yes, calling you now")
        self.assertEqual(row["unread"], 1)

    def test_the_text_is_one_short_line(self) -> None:
        self.say("\n\n  First line \nSecond line")
        self.assertEqual(self.row()["last_message"]["text"], "First line")
        self.say("x" * 2000)
        text = self.row()["last_message"]["text"]
        self.assertEqual(len(text), TILE_TEXT_MAX)
        self.assertTrue(text.endswith("…"))

    def test_a_title_only_note_and_a_task_line(self) -> None:
        self.say(title="USB blocked", sender=Message.Sender.OWNER)
        self.assertEqual(self.row()["last_message"]["text"], "USB blocked")
        self.say("Please use the new logo", title="Send the deck", sender=Message.Sender.OWNER, kind=Message.Kind.TASK)
        self.assertEqual(self.row()["last_message"]["text"], "Task: Send the deck")

    def test_reading_tiles_marks_nothing_read(self) -> None:
        self.say("Hello?")
        self.assertEqual(self.row()["unread"], 1)
        self.assertEqual(self.row()["unread"], 1)
        self.assertTrue(Message.objects.filter(read_by_owner=False).exists())
        self.client.get(f"{MEMBERS_URL}{self.member.id}/messages/")
        row = self.row()
        self.assertEqual(row["unread"], 0)
        self.assertEqual(row["last_message"]["text"], "Hello?")

    def test_one_member_responses_carry_the_same_fields(self) -> None:
        self.say("Hello?")
        self.as_owner()
        row = self.client.patch(f"{MEMBERS_URL}{self.member.id}/", {"role": "sales"}, format="json").json()
        self.assertEqual(row["unread"], 1)
        self.assertEqual(row["last_message"]["text"], "Hello?")


class ChatTilePrivacyTests(DeviceTestCase):
    def setUp(self) -> None:
        super().setUp()
        Message.objects.create(owner=self.owner, member=self.member, sender=Message.Sender.MEMBER, body="private line")

    def test_without_chat_on_the_plan_the_text_stays_out(self) -> None:
        plan = Plan.objects.get(code="businesses")
        plan.modules = [m for m in plan.modules if m != "team_chat"]
        plan.save(update_fields=["modules"])
        self.as_owner()
        self.assertEqual(
            self.client.get(f"{MEMBERS_URL}{self.member.id}/messages/").status_code, status.HTTP_403_FORBIDDEN
        )
        response = self.client.get(MEMBERS_URL)
        self.assertNotIn("private line", response.content.decode())
        self.assertIsNone(response.json()[0]["last_message"])
        self.assertEqual(response.json()[0]["unread"], 1)  # the count was there before, and says nothing of the text

    def test_a_plan_that_ended_keeps_the_text_out_too(self) -> None:
        Plan.objects.filter(code="free").update(modules=["usb_alerts"])
        Subscription.objects.filter(owner=self.owner).delete()
        self.as_owner()
        self.assertNotIn("private line", self.client.get(MEMBERS_URL).content.decode())

    def test_another_account_never_sees_it(self) -> None:
        other = User.objects.create_user("other@acme.in", "pw-12345678")
        give_plan(other, "businesses")
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(MEMBERS_URL).json(), [])

    def test_a_pc_token_cannot_read_the_list(self) -> None:
        token = self.enroll().json()["device_token"]
        self.as_device(token)
        self.assertIn(
            self.client.get(MEMBERS_URL).status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)
        )
        # What the PC reads is its own member's chat only, not the owner's list.
        self.assertEqual(self.client.get(MESSAGES_URL).status_code, status.HTTP_200_OK)

    def test_the_lookup_is_scoped_to_the_owner(self) -> None:
        from .services import chat_tiles

        other = User.objects.create_user("other@acme.in", "pw-12345678")
        give_plan(other, "businesses")
        self.assertIsNone(chat_tiles(other, [self.member])[self.member.pk]["last_message"])


class ChatTileQueryTests(DeviceTestCase):
    def add_people(self, count: int, start: int = 0) -> None:
        for i in range(start, start + count):
            member = Member.objects.create(owner=self.owner, name=f"Person {i}", email=f"p{i}@acme.in")
            Device.objects.create(
                owner=self.owner, member=member, machine_id=f"machine-{i}", hostname=f"PC-{i}",
                token_hash=f"hash-{i}", last_seen_at=timezone.now(),
            )
            for n in range(3):
                Message.objects.create(owner=self.owner, member=member, sender=Message.Sender.MEMBER, body=f"line {n}")

    def queries(self) -> int:
        with CaptureQueriesContext(connection) as context:
            response = self.client.get(MEMBERS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return len(context)

    def test_queries_stay_flat_as_the_team_grows(self) -> None:
        self.as_owner()
        self.add_people(2)
        self.client.get(MEMBERS_URL)
        small = self.queries()
        self.add_people(20, start=2)
        large = self.queries()
        self.assertEqual(large, small)
        rows = self.client.get(MEMBERS_URL).json()
        self.assertEqual(len(rows), 23)
        self.assertEqual(sum(1 for r in rows if r["last_message"] and r["last_message"]["text"] == "line 2"), 22)
        self.assertEqual(sum(r["unread"] for r in rows), 66)
