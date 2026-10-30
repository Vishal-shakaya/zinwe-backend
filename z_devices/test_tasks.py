"""FR-20 v2: tasks (owner endpoints, device endpoints, the task line in both chat threads, plan gate, status rules)."""
from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest import mock

from django.conf import settings
from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token

from z_auth.models import Plan, User

from .authentication import DeviceScopedThrottle
from .models import Member, Message, NotificationPrefs, Task, TaskActivity
from .owner_feed import feed
from .test_plan_limits import SPLIT, PlanTestCase
from .tests import MEMBERS_URL, MESSAGES_URL, DeviceTestCase

TASKS_URL = "/api/devices/tasks/"

TASK_KEYS = {
    "id", "member_id", "member_name", "member_picture", "title", "note", "due_at", "priority", "steps", "status", "help_note",
    "created_at", "updated_at", "status_changed_at", "status_changed_by", "message_id", "activity",
}


def task_url(task_id: int) -> str:
    return f"{TASKS_URL}{task_id}/"


def action_url(task_id: int) -> str:
    return f"{TASKS_URL}{task_id}/action/"


class TaskTestCase(DeviceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.token = self.enroll().json()["device_token"]

    def create(self, member: Member | None = None, **body: Any):  # type: ignore[no-untyped-def]
        self.as_owner()
        payload = {"title": "Reconcile October GST", **body}
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(f"{MEMBERS_URL}{(member or self.member).id}/tasks/", payload, format="json")

    def make(self, **body: Any) -> dict[str, Any]:
        response = self.create(**body)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        return response.json()

    def owner_act(self, task_id: int, action: str, **extra: Any):  # type: ignore[no-untyped-def]
        self.as_owner()
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(action_url(task_id), {"action": action, **extra}, format="json")

    def member_act(self, task_id: int, action: str, token: str | None = None, **extra: Any):  # type: ignore[no-untyped-def]
        self.as_device(token or self.token)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(action_url(task_id), {"action": action, **extra}, format="json")

    def set_status(self, task_id: int, value: str) -> None:
        Task.objects.filter(pk=task_id).update(status=value)

    def other_owner(self) -> tuple[User, Member]:
        from z_auth._testing import give_plan

        other = User.objects.create_user("boss@other.in", "pw-12345678")
        give_plan(other, "businesses")
        return other, Member.objects.create(owner=other, name="Ravi", email="ravi@other.in")


class CreateTaskTests(TaskTestCase):
    def test_create_returns_the_contract_shape_and_posts_a_task_line(self) -> None:
        due = (timezone.now() + timedelta(days=1)).replace(microsecond=0)
        body = self.make(
            note="Match the purchase register with GSTR-2B.", due_at=due.isoformat(), priority="high",
            steps=["Download GSTR-2B", "  Match the register  ", ""],
        )
        self.assertEqual(set(body), TASK_KEYS)
        self.assertEqual(body["member_id"], self.member.id)
        self.assertEqual(body["member_name"], "Asha Rao")
        self.assertEqual(body["status"], "sent")
        self.assertEqual(body["status_changed_by"], "owner")
        self.assertEqual(body["priority"], "high")
        self.assertEqual(body["help_note"], "")
        self.assertEqual([s["title"] for s in body["steps"]], ["Download GSTR-2B", "Match the register"])
        self.assertTrue(all(set(s) == {"id", "title", "done"} and s["done"] is False for s in body["steps"]))
        self.assertEqual([(a["by"], a["action"]) for a in body["activity"]], [("owner", "created")])
        self.assertEqual(set(body["activity"][0]), {"at", "by", "action", "note"})

        message = Message.objects.get(pk=body["message_id"])
        self.assertEqual(message.kind, "task")
        self.assertEqual((message.title, message.body), ("Reconcile October GST", "Match the purchase register with GSTR-2B."))
        self.assertEqual((message.sender, message.channel), ("owner", "chat"))
        self.assertTrue(message.read_by_owner)

    def test_defaults(self) -> None:
        body = self.make()
        self.assertEqual((body["note"], body["due_at"], body["priority"], body["steps"]), ("", None, "normal", []))

    def test_validation(self) -> None:
        self.assertEqual(self.create(title="").status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.create(title="x" * 121).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.create(note="x" * 1001).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.create(priority="urgent").status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.create(due_at="tomorrow").status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.create(steps=[f"s{i}" for i in range(21)]).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Task.objects.exists())
        self.assertFalse(Message.objects.exists())

    def test_switched_off_member_gets_no_task(self) -> None:
        Member.objects.filter(pk=self.member.pk).update(is_active=False)
        response = self.create()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("member", response.json())
        self.assertFalse(Message.objects.exists())

    def test_another_owners_member_is_not_found(self) -> None:
        _, theirs = self.other_owner()
        self.assertEqual(self.create(member=theirs).status_code, status.HTTP_404_NOT_FOUND)

    def test_needs_an_owner_login(self) -> None:
        url = f"{MEMBERS_URL}{self.member.id}/tasks/"
        self.assertEqual(self.client.post(url, {"title": "x"}, format="json").status_code, status.HTTP_401_UNAUTHORIZED)
        self.as_device(self.token)
        self.assertEqual(self.client.post(url, {"title": "x"}, format="json").status_code, status.HTTP_401_UNAUTHORIZED)

    def test_task_line_carries_the_task_in_both_threads(self) -> None:
        task = self.make(steps=["One"])
        self.as_owner()
        self.client.post(f"{MEMBERS_URL}{self.member.id}/message/", {"title": "Hi"}, format="json")
        thread = self.client.get(f"{MEMBERS_URL}{self.member.id}/messages/").json()
        by_kind = {m["kind"]: m for m in thread}
        self.assertIsNone(by_kind["chat"]["task"])
        self.assertEqual(by_kind["task"]["task"]["id"], task["id"])
        self.assertEqual(set(by_kind["task"]["task"]), TASK_KEYS)
        # The member's PC sees the same line with the task (an older window shows title + body).
        self.as_device(self.token)
        lines = self.client.get(MESSAGES_URL).json()
        line = next(m for m in lines if m["kind"] == "task")
        self.assertEqual(line["title"], "Reconcile October GST")
        self.assertEqual(line["task"]["steps"][0]["title"], "One")
        # The card is live: after the member starts, the thread shows the new status.
        self.member_act(task["id"], "start")
        self.as_owner()
        thread = self.client.get(f"{MEMBERS_URL}{self.member.id}/messages/").json()
        self.assertEqual(next(m for m in thread if m["kind"] == "task")["task"]["status"], "progress")

    def test_thread_queries_do_not_grow_with_tasks(self) -> None:
        for i in range(3):
            self.make(title=f"Task {i}", steps=["a", "b"])
        self.as_owner()
        url = f"{MEMBERS_URL}{self.member.id}/messages/"
        self.client.get(url)
        with self.assertNumQueries(7):  # token, user, plan, mark read, messages, steps, activity
            self.client.get(url)


class OwnerListTests(TaskTestCase):
    def test_status_filters_and_newest_activity_first(self) -> None:
        a = self.make(title="A")
        b = self.make(title="B")
        c = self.make(title="C")
        self.owner_act(c["id"], "approve")
        self.owner_act(b["id"], "cancel")
        self.member_act(a["id"], "start")  # a has the newest activity now
        self.as_owner()
        self.assertEqual([t["title"] for t in self.client.get(TASKS_URL).json()], ["A"])
        self.assertEqual([t["title"] for t in self.client.get(TASKS_URL, {"status": "open"}).json()], ["A"])
        self.assertEqual({t["title"] for t in self.client.get(TASKS_URL, {"status": "done"}).json()}, {"B", "C"})
        self.assertEqual([t["title"] for t in self.client.get(TASKS_URL, {"status": "all"}).json()], ["A", "B", "C"])

    def test_member_filter(self) -> None:
        other = Member.objects.create(owner=self.owner, name="Ben", email="ben@acme.in")
        self.make(title="Asha's")
        self.make(member=other, title="Ben's")
        self.as_owner()
        rows = self.client.get(TASKS_URL, {"member": other.id}).json()
        self.assertEqual([(t["title"], t["member_name"]) for t in rows], [("Ben's", "Ben")])

    def test_bad_query(self) -> None:
        self.as_owner()
        self.assertEqual(self.client.get(TASKS_URL, {"status": "late"}).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.client.get(TASKS_URL, {"member": "x"}).status_code, status.HTTP_400_BAD_REQUEST)

    def test_another_owner_sees_none_of_them(self) -> None:
        task = self.make()
        other, _ = self.other_owner()
        token, _ = Token.objects.get_or_create(user=other)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        self.assertEqual(self.client.get(TASKS_URL, {"status": "all"}).json(), [])
        self.assertEqual(self.client.get(TASKS_URL, {"member": self.member.id}).json(), [])
        self.assertEqual(self.client.patch(task_url(task["id"]), {"title": "x"}, format="json").status_code, 404)
        self.assertEqual(self.client.post(action_url(task["id"]), {"action": "approve"}, format="json").status_code, 404)

    def test_needs_login(self) -> None:
        self.assertEqual(self.client.get(TASKS_URL).status_code, status.HTTP_401_UNAUTHORIZED)


class EditTaskTests(TaskTestCase):
    def test_edit_fields_updates_the_chat_line(self) -> None:
        task = self.make(note="old")
        self.as_owner()
        due = (timezone.now() + timedelta(days=2)).replace(microsecond=0)
        body = self.client.patch(
            task_url(task["id"]), {"title": "New title", "note": "new", "priority": "high", "due_at": due.isoformat()},
            format="json",
        ).json()
        self.assertEqual((body["title"], body["note"], body["priority"]), ("New title", "new", "high"))
        self.assertIsNotNone(body["due_at"])
        self.assertEqual(body["activity"][-1]["action"], "edited")
        self.assertEqual(body["status"], "sent")  # editing doesn't move the status
        message = Message.objects.get(pk=task["message_id"])
        self.assertEqual((message.title, message.body), ("New title", "new"))
        # Clearing the due date.
        self.assertIsNone(self.client.patch(task_url(task["id"]), {"due_at": None}, format="json").json()["due_at"])

    def test_steps_keep_their_ticks(self) -> None:
        task = self.make(steps=["One", "Two", "Three"])
        one, two, _ = task["steps"]
        self.member_act(task["id"], "note", step_id=two["id"], step_done=True)
        self.as_owner()
        body = self.client.patch(
            task_url(task["id"]), {"steps": [{"id": two["id"], "title": "Two (renamed)"}, "One", "Four"]}, format="json"
        ).json()
        self.assertEqual(
            [(s["title"], s["done"]) for s in body["steps"]],
            [("Two (renamed)", True), ("One", False), ("Four", False)],
        )
        self.assertEqual(body["steps"][1]["id"], one["id"])

    def test_nothing_changed_writes_no_activity(self) -> None:
        task = self.make(steps=["One"])
        self.as_owner()
        body = self.client.patch(task_url(task["id"]), {"title": task["title"], "steps": ["One"]}, format="json").json()
        self.assertEqual([a["action"] for a in body["activity"]], ["created"])

    def test_validation_and_cancelled(self) -> None:
        task = self.make()
        self.as_owner()
        self.assertEqual(self.client.patch(task_url(task["id"]), {"title": ""}, format="json").status_code, 400)
        self.assertEqual(self.client.patch(task_url(task["id"]), {"steps": [3]}, format="json").status_code, 400)
        self.assertEqual(self.client.patch(task_url(task["id"]), {"steps": [{"title": "x", "done": "yes"}]}, format="json").status_code, 400)
        self.owner_act(task["id"], "cancel")
        response = self.client.patch(task_url(task["id"]), {"title": "x"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("status", response.json())

    def test_member_pc_cannot_edit(self) -> None:
        task = self.make()
        self.as_device(self.token)
        self.assertEqual(self.client.patch(task_url(task["id"]), {"title": "x"}, format="json").status_code, 401)

    def test_unknown_task(self) -> None:
        self.as_owner()
        self.assertEqual(self.client.patch(task_url(999), {"title": "x"}, format="json").status_code, 404)


class OwnerActionTests(TaskTestCase):
    def test_mark_done_goes_straight_to_approved(self) -> None:
        task = self.make()
        self.member_act(task["id"], "start")
        body = self.owner_act(task["id"], "approve", note="Thanks!").json()
        self.assertEqual((body["status"], body["status_changed_by"]), ("approved", "owner"))
        self.assertEqual(body["activity"][-1], {**body["activity"][-1], "by": "owner", "action": "approved", "note": "Thanks!"})

    def test_approve_after_review(self) -> None:
        task = self.make()
        self.member_act(task["id"], "done", note="All matched.")
        self.assertEqual(self.owner_act(task["id"], "approve").json()["status"], "approved")
        # Again: a no-op, not an error, and no second activity line.
        again = self.owner_act(task["id"], "approve")
        self.assertEqual(again.status_code, 200)
        self.assertEqual([a["action"] for a in again.json()["activity"]].count("approved"), 1)

    def test_reopen(self) -> None:
        task = self.make()
        self.assertEqual(self.owner_act(task["id"], "reopen").status_code, 400)  # still open
        self.owner_act(task["id"], "approve")
        body = self.owner_act(task["id"], "reopen", note="Please check the last step again.").json()
        self.assertEqual(body["status"], "progress")
        self.assertEqual(body["activity"][-1]["note"], "Please check the last step again.")
        # Answering a help request reopens it too.
        self.member_act(task["id"], "help", note="Which folder?")
        self.assertEqual(self.owner_act(task["id"], "reopen").json()["status"], "progress")

    def test_cancelled_is_final_until_reopened(self) -> None:
        task = self.make()
        self.assertEqual(self.owner_act(task["id"], "cancel").json()["status"], "cancelled")
        self.assertEqual(self.owner_act(task["id"], "cancel").status_code, 200)  # no-op
        response = self.owner_act(task["id"], "approve")
        self.assertEqual(response.status_code, 400)
        self.assertIn("action", response.json())
        self.assertEqual(self.owner_act(task["id"], "nudge").status_code, 400)
        for action in ("start", "done", "help", "accept"):
            self.assertEqual(self.member_act(task["id"], action, note="x").status_code, 400)
        self.assertEqual(self.owner_act(task["id"], "reopen").json()["status"], "progress")

    def test_approved_cannot_be_cancelled(self) -> None:
        task = self.make()
        self.owner_act(task["id"], "approve")
        self.assertEqual(self.owner_act(task["id"], "cancel").status_code, 400)

    def test_owner_cannot_use_member_actions(self) -> None:
        task = self.make()
        for action in ("start", "seen", "accept", "done", "help", "note", "nonsense"):
            self.assertEqual(self.owner_act(task["id"], action).status_code, 400, action)
        self.assertEqual(Task.objects.get().status, "sent")

    def test_nudge_once_an_hour(self) -> None:
        task = self.make()
        body = self.owner_act(task["id"], "nudge").json()
        self.assertEqual((body["status"], body["activity"][-1]["action"]), ("sent", "nudged"))
        self.assertEqual(self.owner_act(task["id"], "nudge").status_code, 400)
        Task.objects.filter(pk=task["id"]).update(nudged_at=timezone.now() - timedelta(hours=2))
        self.assertEqual(self.owner_act(task["id"], "nudge").status_code, 200)

    def test_nothing_to_nudge_once_done(self) -> None:
        task = self.make()
        self.member_act(task["id"], "done")
        self.assertEqual(self.owner_act(task["id"], "nudge").status_code, 400)

    def test_unknown_task(self) -> None:
        self.assertEqual(self.owner_act(999, "approve").status_code, 404)


class DeviceTaskTests(TaskTestCase):
    def test_device_list_open_plus_recently_approved(self) -> None:
        open_task = self.make(title="Open")
        recent = self.make(title="Approved recently")
        old = self.make(title="Approved long ago")
        cancelled = self.make(title="Cancelled")
        self.owner_act(recent["id"], "approve")
        self.owner_act(old["id"], "approve")
        Task.objects.filter(pk=old["id"]).update(status_changed_at=timezone.now() - timedelta(days=8))
        self.owner_act(cancelled["id"], "cancel")
        # A task for someone else at the same company never shows on this PC.
        other = Member.objects.create(owner=self.owner, name="Ben", email="ben@acme.in")
        self.make(member=other, title="Ben's")
        self.as_device(self.token)
        rows = self.client.get(TASKS_URL).json()
        self.assertEqual({t["title"] for t in rows}, {"Open", "Approved recently"})
        self.assertEqual(set(rows[0]), TASK_KEYS)
        self.assertIn(open_task["id"], {t["id"] for t in rows})

    def test_full_member_flow(self) -> None:
        task = self.make(steps=["One", "Two"])
        seen = self.member_act(task["id"], "seen").json()
        self.assertEqual((seen["status"], seen["status_changed_by"]), ("seen", "member"))
        self.assertTrue(Message.objects.get(pk=task["message_id"]).read_by_member)
        self.assertEqual(self.member_act(task["id"], "accept").json()["status"], "accepted")
        self.assertEqual(self.member_act(task["id"], "start").json()["status"], "progress")
        helped = self.member_act(task["id"], "help", note="The file is view-only.").json()
        self.assertEqual((helped["status"], helped["help_note"]), ("help", "The file is view-only."))
        self.assertEqual(self.member_act(task["id"], "start").json()["status"], "progress")
        step = task["steps"][0]["id"]
        ticked = self.member_act(task["id"], "note", step_id=step, step_done=True).json()
        self.assertTrue(ticked["steps"][0]["done"])
        self.assertEqual(ticked["activity"][-1], {**ticked["activity"][-1], "by": "member", "action": "step_done", "note": "One"})
        noted = self.member_act(task["id"], "note", note="Half way.").json()
        self.assertEqual(noted["activity"][-1]["action"], "note")
        done = self.member_act(task["id"], "done", note="Sent to the CA.").json()
        self.assertEqual(done["status"], "review")
        self.assertEqual(self.owner_act(task["id"], "approve").json()["status"], "approved")
        actions = [a.action for a in TaskActivity.objects.filter(task_id=task["id"]).order_by("at", "id")]
        self.assertEqual(
            actions,
            ["created", "seen", "accepted", "started", "help", "started", "step_done", "note", "done", "approved"],
        )

    def test_repeats_are_no_ops(self) -> None:
        task = self.make()
        self.member_act(task["id"], "seen")
        self.member_act(task["id"], "seen")
        self.member_act(task["id"], "start")
        self.member_act(task["id"], "seen")  # seen after starting: nothing moves back
        self.member_act(task["id"], "start")
        body = self.member_act(task["id"], "done").json()
        self.assertEqual(self.member_act(task["id"], "done").status_code, 200)
        self.assertEqual([a["action"] for a in body["activity"]], ["created", "seen", "started", "done"])

    def test_step_toggle_without_step_done_flips_it(self) -> None:
        task = self.make(steps=["One"])
        step = task["steps"][0]["id"]
        self.assertTrue(self.member_act(task["id"], "note", step_id=step).json()["steps"][0]["done"])
        self.assertFalse(self.member_act(task["id"], "note", step_id=step).json()["steps"][0]["done"])

    def test_member_validation(self) -> None:
        task = self.make(steps=["One"])
        self.assertEqual(self.member_act(task["id"], "help").status_code, 400)  # needs a note
        self.assertEqual(self.member_act(task["id"], "note").status_code, 400)  # needs a note or a step
        self.assertEqual(self.member_act(task["id"], "note", step_id=99999).status_code, 400)
        self.assertEqual(self.member_act(task["id"], "note", note="x" * 1001).status_code, 400)

    def test_member_cannot_approve_reopen_cancel_or_nudge(self) -> None:
        task = self.make()
        self.member_act(task["id"], "done")
        for action in ("approve", "reopen", "cancel", "nudge"):
            self.assertEqual(self.member_act(task["id"], action).status_code, 400, action)
        self.assertEqual(Task.objects.get().status, "review")

    def test_closed_task_takes_no_member_changes(self) -> None:
        task = self.make(steps=["One"])
        self.owner_act(task["id"], "approve")
        self.assertEqual(self.member_act(task["id"], "start").status_code, 400)
        self.assertEqual(self.member_act(task["id"], "note", note="hi").status_code, 400)
        self.assertEqual(self.member_act(task["id"], "seen").status_code, 200)  # harmless

    def test_another_members_task_is_not_found(self) -> None:
        other = Member.objects.create(owner=self.owner, name="Ben", email="ben@acme.in")
        theirs = self.make(member=other)
        self.assertEqual(self.member_act(theirs["id"], "start").status_code, 404)
        self.assertEqual(Task.objects.get(pk=theirs["id"]).status, "sent")

    def test_another_companys_pc_is_not_found(self) -> None:
        task = self.make()
        other, theirs = self.other_owner()
        token = self.enroll(email=theirs.email, member_id=theirs.member_code, machine_id="OTHER-PC").json()["device_token"]
        self.assertEqual(self.member_act(task["id"], "start", token=token).status_code, 404)
        self.as_device(token)
        self.assertEqual(self.client.get(TASKS_URL).json(), [])

    def test_pc_without_member_has_no_tasks(self) -> None:
        from .models import Device

        Device.objects.update(member=None)
        self.as_device(self.token)
        self.assertEqual(self.client.get(TASKS_URL).json(), [])

    def test_bad_token(self) -> None:
        self.as_device("not-a-token")
        self.assertEqual(self.client.get(TASKS_URL).status_code, 401)

    def test_device_actions_throttled_per_pc(self) -> None:
        self.assertEqual(settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["device_tasks"], "120/min")
        task = self.make()
        with mock.patch.dict(DeviceScopedThrottle.THROTTLE_RATES, {"device_tasks": "3/min"}):
            for _ in range(3):
                self.assertNotEqual(self.member_act(task["id"], "seen").status_code, 429)
            self.assertEqual(self.member_act(task["id"], "seen").status_code, status.HTTP_429_TOO_MANY_REQUESTS)
            # The owner isn't throttled by the PC's limit.
            for _ in range(5):
                self.assertEqual(self.owner_act(task["id"], "nudge").status_code in (200, 400), True)


class TaskNotificationTests(TaskTestCase):
    def test_help_and_done_reach_the_owner_feed(self) -> None:
        since = timezone.now() - timedelta(seconds=1)
        first = self.make(title="Export the screens")
        second = self.make(title="Ship the fix")
        self.member_act(first["id"], "seen")
        self.member_act(first["id"], "help", note="Need edit access.")
        self.member_act(second["id"], "done")
        items = [i for i in feed(self.owner, since)["items"] if i["kind"] == "task"]
        self.assertEqual(
            {(i["title"], i["body"]) for i in items},
            {
                ("Asha Rao needs help with a task", "Export the screens: Need edit access."),
                ("Asha Rao finished a task", "Ship the fix"),
            },
        )
        self.assertTrue(all(i["link"].startswith("/app/tasks?task=") for i in items))

    def test_owner_can_switch_task_notifications_off(self) -> None:
        since = timezone.now() - timedelta(seconds=1)
        task = self.make()
        self.member_act(task["id"], "done")
        NotificationPrefs.objects.create(owner=self.owner, tasks=False)
        self.assertEqual([i for i in feed(self.owner, since)["items"] if i["kind"] == "task"], [])

    def test_prefs_api_has_the_tasks_switch(self) -> None:
        self.as_owner()
        url = "/api/devices/notifications/settings/"
        self.assertTrue(self.client.get(url).json()["tasks"])
        self.assertFalse(self.client.patch(url, {"tasks": False}, format="json").json()["tasks"])


class TaskPlanGateTests(PlanTestCase):
    """Tasks ride on the team_chat module, like chat."""

    def setUp(self) -> None:
        super().setUp()
        self.token = self.enroll().json()["device_token"]
        self.as_owner()
        self.task_id = self.client.post(f"{MEMBERS_URL}{self.member.id}/tasks/", {"title": "x"}, format="json").json()["id"]
        self.use_plan("startups")  # no team_chat in the split

    def test_owner_endpoints_locked(self) -> None:
        self.assertNotIn("team_chat", Plan.objects.get(code="startups").modules)
        self.assertEqual(SPLIT["startups"], Plan.objects.get(code="startups").modules)
        self.as_owner()
        for response in (
            self.client.get(TASKS_URL),
            self.client.post(f"{MEMBERS_URL}{self.member.id}/tasks/", {"title": "x"}, format="json"),
            self.client.patch(task_url(self.task_id), {"title": "y"}, format="json"),
            self.client.post(action_url(self.task_id), {"action": "approve"}, format="json"),
        ):
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
            self.assertEqual(response.json()["module"], "team_chat")

    def test_device_sees_no_tasks_and_cannot_act(self) -> None:
        self.as_device(self.token)
        self.assertEqual(self.client.get(TASKS_URL).json(), [])
        self.assertEqual(
            self.client.post(action_url(self.task_id), {"action": "start"}, format="json").status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(Task.objects.get().status, "sent")

    def test_no_task_notifications_without_the_module(self) -> None:
        since = timezone.now() - timedelta(seconds=1)
        Task.objects.update(status="progress")
        TaskActivity.objects.create(task_id=self.task_id, by="member", action="done")
        self.assertEqual([i for i in feed(self.owner, since)["items"] if i["kind"] == "task"], [])
