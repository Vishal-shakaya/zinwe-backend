"""A member's own to-dos: device writes, owner reads, permissions, plan gate, validation, last-write-wins, deletes."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status

from z_auth.models import User

from .models import Device, Member, MemberTodo
from .test_plan_limits import PlanTestCase
from .tests import MEMBERS_URL, DeviceTestCase

TODOS_URL = "/api/devices/todos/"
CLIENT_ID = "0123456789abcdef0123456789abcdef"

TODO_KEYS = {
    "id", "client_id", "member_id", "member_name", "title", "due_date", "steps", "status", "done_at", "created_at",
    "updated_at", "deleted",
}


def todo_url(client_id: str = CLIENT_ID) -> str:
    return f"{TODOS_URL}{client_id}/"


def stamp(minutes: int = 0) -> datetime:
    return (timezone.now() + timedelta(minutes=minutes)).replace(microsecond=0)


class TodoTestCase(DeviceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.token = self.enroll().json()["device_token"]

    def put(self, at: datetime | None = None, client_id: str = CLIENT_ID, token: str | None = None, **body: Any):  # type: ignore[no-untyped-def]
        self.as_device(token or self.token)
        payload = {"title": "Update the client tracker", "updated_at": (at or stamp()).isoformat(), **body}
        return self.client.put(todo_url(client_id), payload, format="json")

    def delete(self, at: datetime | None = None, client_id: str = CLIENT_ID, token: str | None = None):  # type: ignore[no-untyped-def]
        self.as_device(token or self.token)
        return self.client.delete(todo_url(client_id), {"updated_at": (at or stamp()).isoformat()}, format="json")

    def other_owner(self) -> tuple[User, Member, str]:
        from z_auth._testing import give_plan

        other = User.objects.create_user("boss@other.in", "pw-12345678")
        give_plan(other, "businesses")
        theirs = Member.objects.create(owner=other, name="Ravi", email="ravi@other.in")
        token = self.enroll(email=theirs.email, member_id=theirs.member_code, machine_id="OTHER-PC").json()["device_token"]
        return other, theirs, token

    def as_user(self, user: User) -> None:
        from rest_framework.authtoken.models import Token

        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")


class HappyPathTests(TodoTestCase):
    def test_save_returns_the_contract_shape(self) -> None:
        at = stamp()
        response = self.put(
            at, due_date="2026-11-02", status="open",
            steps=[{"title": " Open the sheet ", "done": True}, {"title": "Send it"}],
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        body = response.json()
        self.assertEqual(set(body), TODO_KEYS)
        self.assertEqual((body["client_id"], body["member_id"], body["member_name"]), (CLIENT_ID, self.member.id, "Asha Rao"))
        self.assertEqual(body["title"], "Update the client tracker")
        self.assertEqual(body["due_date"], "2026-11-02")
        self.assertEqual(body["steps"], [{"title": "Open the sheet", "done": True}, {"title": "Send it", "done": False}])
        self.assertEqual((body["status"], body["done_at"], body["deleted"]), ("open", None, False))
        row = MemberTodo.objects.get()
        self.assertEqual((row.owner, row.member, row.updated_at), (self.owner, self.member, at))

    def test_the_windows_apps_exact_bytes(self) -> None:
        """What BackendClient.SendTodoAsync really sends (see TodosApiTests): the contract between the two repos."""
        self.as_device(self.token)
        put = self.client.put(
            todo_url(),
            '{"title":"Call Karan","due_date":"2026-11-02","steps":[{"title":"Find the number","done":true}],'
            '"status":"open","updated_at":"2026-10-30T09:00:00.125+00:00"}',
            content_type="application/json; charset=utf-8",
        )
        self.assertEqual(put.status_code, status.HTTP_201_CREATED, put.content)
        self.assertEqual(MemberTodo.objects.get().updated_at.isoformat(), "2026-10-30T09:00:00.125000+00:00")
        gone = self.client.delete(
            todo_url(), '{"updated_at":"2026-10-30T09:01:00+00:00"}', content_type="application/json; charset=utf-8"
        )
        self.assertTrue(gone.json()["deleted"])

    def test_defaults(self) -> None:
        body = self.put().json()
        self.assertEqual((body["due_date"], body["steps"], body["status"]), (None, [], "open"))

    def test_done_sets_done_at_and_keeps_it_and_reopening_clears_it(self) -> None:
        first = stamp(-3)
        self.put(first)
        ticked = stamp(-2)
        done_at = self.put(ticked, status="done").json()["done_at"]
        self.assertEqual(datetime.fromisoformat(done_at.replace("Z", "+00:00")), ticked)
        self.assertEqual(self.put(stamp(-1), status="done", title="New name").json()["done_at"], done_at)
        self.assertIsNone(self.put(stamp(), status="open").json()["done_at"])

    def test_device_list_is_the_members_own_with_deleted_ones(self) -> None:
        self.put(client_id="aaaaaaaa11111111", title="Keep")
        self.put(client_id="bbbbbbbb22222222", title="Gone")
        self.delete(client_id="bbbbbbbb22222222")
        other = Member.objects.create(owner=self.owner, name="Ben", email="ben@acme.in")
        MemberTodo.objects.create(owner=self.owner, member=other, client_id="cccccccc33333333", title="Ben's", updated_at=stamp())
        self.as_device(self.token)
        body = self.client.get(TODOS_URL).json()
        self.assertEqual(body["member_id"], self.member.id)
        self.assertEqual({t["title"]: t["deleted"] for t in body["todos"]}, {"Keep": False, "": True})
        self.assertEqual(set(body["todos"][0]), TODO_KEYS)

    def test_owner_sees_them_read_only_and_never_the_deleted_ones(self) -> None:
        self.put(client_id="aaaaaaaa11111111", title="Open one", at=stamp(-3))
        self.put(client_id="bbbbbbbb22222222", title="Done one", status="done", at=stamp(-2))
        self.put(client_id="cccccccc33333333", title="Deleted one", at=stamp(-5))
        self.delete(client_id="cccccccc33333333", at=stamp(-1))
        self.as_owner()
        rows = self.client.get(TODOS_URL).json()
        self.assertEqual([t["title"] for t in rows], ["Done one", "Open one"])
        self.assertEqual(set(rows[0]), TODO_KEYS)
        self.assertEqual([t["title"] for t in self.client.get(TODOS_URL, {"status": "open"}).json()], ["Open one"])
        self.assertEqual([t["title"] for t in self.client.get(TODOS_URL, {"status": "done"}).json()], ["Done one"])
        one = self.client.get(f"{MEMBERS_URL}{self.member.id}/todos/").json()
        self.assertEqual([t["title"] for t in one], ["Done one", "Open one"])

    def test_owner_list_filters_by_member(self) -> None:
        self.put()
        other = Member.objects.create(owner=self.owner, name="Ben", email="ben@acme.in")
        MemberTodo.objects.create(owner=self.owner, member=other, client_id="cccccccc33333333", title="Ben's", updated_at=stamp())
        self.as_owner()
        rows = self.client.get(TODOS_URL, {"member": other.id}).json()
        self.assertEqual([t["member_name"] for t in rows], ["Ben"])

    def test_a_new_pc_gets_the_same_list(self) -> None:
        self.put(title="Follows me", due_date="2026-11-02", steps=[{"title": "One", "done": True}])
        token = self.enroll(machine_id="SECOND-PC", hostname="HOME-PC").json()["device_token"]
        self.as_device(token)
        todos = self.client.get(TODOS_URL).json()["todos"]
        self.assertEqual([(t["title"], t["steps"]) for t in todos], [("Follows me", [{"title": "One", "done": True}])])


class IdempotencyTests(TodoTestCase):
    def test_the_same_write_twice_is_one_row(self) -> None:
        at = stamp()
        self.assertEqual(self.put(at).status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.put(at).status_code, status.HTTP_200_OK)
        self.assertEqual(MemberTodo.objects.count(), 1)

    def test_delete_twice_is_one_tombstone(self) -> None:
        self.put(stamp(-1))
        at = stamp()
        self.assertTrue(self.delete(at).json()["deleted"])
        self.assertTrue(self.delete(at).json()["deleted"])
        self.assertEqual(MemberTodo.objects.count(), 1)

    def test_client_id_is_unique_per_member_not_across_members(self) -> None:
        with self.assertRaises(IntegrityError), transaction.atomic():
            MemberTodo.objects.create(owner=self.owner, member=self.member, client_id=CLIENT_ID, updated_at=stamp())
            MemberTodo.objects.create(owner=self.owner, member=self.member, client_id=CLIENT_ID, updated_at=stamp())
        _, theirs, token = self.other_owner()
        self.put(title="Mine")
        self.put(title="Theirs", token=token)
        self.assertEqual(
            {(t.member_id, t.title) for t in MemberTodo.objects.all()}, {(self.member.id, "Mine"), (theirs.id, "Theirs")}
        )


class LastWriteWinsTests(TodoTestCase):
    def test_a_newer_write_wins(self) -> None:
        self.put(stamp(-2), title="Old")
        body = self.put(stamp(-1), title="New").json()
        self.assertEqual(body["title"], "New")

    def test_an_older_write_changes_nothing_and_gets_the_row_back(self) -> None:
        newer = stamp(-1)
        self.put(newer, title="New")
        response = self.put(stamp(-2), title="Old", status="done")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual((response.json()["title"], response.json()["status"]), ("New", "open"))
        self.assertEqual(MemberTodo.objects.get().updated_at, newer)

    def test_the_same_time_is_not_newer(self) -> None:
        at = stamp()
        self.put(at, title="First")
        self.assertEqual(self.put(at, title="Second").json()["title"], "First")

    def test_a_clock_far_ahead_counts_as_now(self) -> None:
        self.put(timezone.now() + timedelta(days=30), title="Far ahead")
        row = MemberTodo.objects.get()
        self.assertLess(row.updated_at, timezone.now() + timedelta(minutes=1))
        self.assertEqual(self.put(stamp(1), title="A real edit").json()["title"], "A real edit")

    def test_a_small_clock_difference_is_kept_exactly(self) -> None:
        at = timezone.now() + timedelta(minutes=2)
        self.put(at)
        self.assertEqual(MemberTodo.objects.get().updated_at, at)


class DeleteTests(TodoTestCase):
    def test_delete_erases_the_words_and_keeps_an_empty_marker(self) -> None:
        self.put(stamp(-1), title="Call the doctor", steps=[{"title": "Find the number", "done": False}])
        at = stamp()
        body = self.delete(at).json()
        self.assertTrue(body["deleted"])
        row = MemberTodo.objects.get()
        self.assertEqual((row.title, row.steps, row.due_date, row.deleted_at, row.updated_at), ("", [], None, at, at))

    def test_undo_brings_it_back_with_a_newer_save(self) -> None:
        self.put(stamp(-5), title="Call the doctor")
        self.delete(stamp(-3))
        body = self.put(stamp(-1), title="Call the doctor").json()
        self.assertEqual((body["title"], body["deleted"]), ("Call the doctor", False))

    def test_delete_wins_over_an_older_edit_that_arrives_late(self) -> None:
        self.put(stamp(-5))
        self.delete(stamp(-1))
        body = self.put(stamp(-2), title="Late edit").json()
        self.assertTrue(body["deleted"])
        self.assertEqual(body["title"], "")

    def test_delete_wins_a_tie(self) -> None:
        at = stamp(-1)
        self.put(at)
        self.assertTrue(self.delete(at).json()["deleted"])
        self.assertTrue(self.put(at, title="Same time").json()["deleted"])

    def test_an_older_delete_loses_to_a_newer_edit(self) -> None:
        self.put(stamp(-1), title="Still wanted")
        body = self.delete(stamp(-2)).json()
        self.assertFalse(body["deleted"])
        self.assertIsNone(MemberTodo.objects.get().deleted_at)

    def test_a_newer_edit_brings_it_back(self) -> None:
        self.put(stamp(-3))
        self.delete(stamp(-2))
        body = self.put(stamp(-1), title="Undo").json()
        self.assertEqual((body["deleted"], body["title"]), (False, "Undo"))

    def test_deleting_an_id_never_seen_keeps_an_empty_tombstone(self) -> None:
        at = stamp(-1)
        body = self.delete(at).json()
        self.assertEqual((body["deleted"], body["title"]), (True, ""))
        self.assertTrue(self.put(stamp(-2), title="Older, from another PC").json()["deleted"])
        self.as_owner()
        self.assertEqual(self.client.get(TODOS_URL).json(), [])


class ValidationTests(TodoTestCase):
    def assert_bad(self, field: str, **body: Any) -> None:
        response = self.put(**body)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, body)
        self.assertIn(field, response.json())
        self.assertFalse(MemberTodo.objects.exists())

    def test_title(self) -> None:
        self.assert_bad("title", title="")
        self.assert_bad("title", title="   ")
        self.assert_bad("title", title="x" * 201)

    def test_due_date_and_status(self) -> None:
        self.assert_bad("due_date", due_date="tomorrow")
        self.assert_bad("status", status="review")

    def test_steps(self) -> None:
        self.assert_bad("steps", steps=[{"title": f"s{i}"} for i in range(51)])
        self.assert_bad("steps", steps=["just text"])
        self.assert_bad("steps", steps=[{"title": "x" * 201}])
        self.assert_bad("steps", steps=[{"title": ""}])
        self.assert_bad("steps", steps=[{"title": "ok", "done": "maybe"}])

    def test_updated_at_is_required_on_save_and_delete(self) -> None:
        self.as_device(self.token)
        response = self.client.put(todo_url(), {"title": "x"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("updated_at", response.json())
        self.assert_bad("updated_at", updated_at="yesterday")
        response = self.client.delete(todo_url(), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("updated_at", response.json())

    def test_client_id(self) -> None:
        for bad in ("short", "x" * 65, "has%20space12", "dots.dots.dots"):
            response = self.put(client_id=bad)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, bad)
            self.assertEqual(self.delete(client_id=bad).status_code, status.HTTP_400_BAD_REQUEST, bad)
        self.assertFalse(MemberTodo.objects.exists())

    def test_owner_query(self) -> None:
        self.as_owner()
        self.assertEqual(self.client.get(TODOS_URL, {"status": "soon"}).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            self.client.get(f"{MEMBERS_URL}{self.member.id}/todos/", {"status": "soon"}).status_code,
            status.HTTP_400_BAD_REQUEST,
        )


class PermissionTests(TodoTestCase):
    """anonymous / the owner / another owner / the member's PC / another owner's PC, on each endpoint."""

    def setUp(self) -> None:
        super().setUp()
        self.put(stamp(-1), title="Asha's")
        self.member_url = f"{MEMBERS_URL}{self.member.id}/todos/"
        self.other, self.theirs, self.their_token = self.other_owner()

    def test_anonymous(self) -> None:
        self.client.credentials()
        for response in (
            self.client.get(TODOS_URL),
            self.client.get(self.member_url),
            self.client.put(todo_url(), {"title": "x", "updated_at": stamp().isoformat()}, format="json"),
            self.client.delete(todo_url(), {"updated_at": stamp().isoformat()}, format="json"),
        ):
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_the_owner_can_only_read(self) -> None:
        self.as_owner()
        self.assertEqual(self.client.get(TODOS_URL).status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.get(self.member_url).status_code, status.HTTP_200_OK)
        put = self.client.put(todo_url(), {"title": "Edited", "updated_at": stamp(1).isoformat()}, format="json")
        delete = self.client.delete(todo_url(), {"updated_at": stamp(1).isoformat()}, format="json")
        self.assertEqual(put.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(delete.status_code, status.HTTP_401_UNAUTHORIZED)
        for method in ("post", "patch", "put", "delete"):
            self.assertEqual(getattr(self.client, method)(self.member_url, {}, format="json").status_code, 405)
            self.assertEqual(getattr(self.client, method)(TODOS_URL, {}, format="json").status_code, 405)
        row = MemberTodo.objects.get(member=self.member)
        self.assertEqual((row.title, row.deleted_at), ("Asha's", None))

    def test_another_owner_sees_nothing(self) -> None:
        self.as_user(self.other)
        self.assertEqual(self.client.get(TODOS_URL).json(), [])
        self.assertEqual(self.client.get(TODOS_URL, {"member": self.member.id}).json(), [])
        self.assertEqual(self.client.get(self.member_url).status_code, status.HTTP_404_NOT_FOUND)

    def test_the_members_pc_cannot_use_the_owner_endpoint(self) -> None:
        self.as_device(self.token)
        self.assertEqual(self.client.get(self.member_url).status_code, status.HTTP_401_UNAUTHORIZED)
        # A PC's list ignores the owner's filters: it's always the PC's own member.
        self.assertEqual(self.client.get(f"{TODOS_URL}?member={self.theirs.id}").json()["member_id"], self.member.id)

    def test_another_owners_pc_never_touches_this_member(self) -> None:
        self.as_device(self.their_token)
        self.assertEqual(self.client.get(self.member_url).status_code, status.HTTP_401_UNAUTHORIZED)
        body = self.client.get(f"{TODOS_URL}?member={self.member.id}").json()
        self.assertEqual((body["member_id"], body["todos"]), (self.theirs.id, []))
        self.put(stamp(), title="Hijack", token=self.their_token)
        self.delete(stamp(1), token=self.their_token)
        row = MemberTodo.objects.get(member=self.member)
        self.assertEqual((row.title, row.deleted_at), ("Asha's", None))

    def test_a_colleagues_pc_never_touches_this_member(self) -> None:
        ben = Member.objects.create(owner=self.owner, name="Ben", email="ben@acme.in")
        token = self.enroll(email=ben.email, member_id=ben.member_code, machine_id="BEN-PC").json()["device_token"]
        self.as_device(token)
        self.assertEqual(self.client.get(TODOS_URL).json()["todos"], [])
        self.delete(stamp(1), token=token)
        self.assertIsNone(MemberTodo.objects.get(member=self.member).deleted_at)

    def test_bad_token(self) -> None:
        self.as_device("not-a-token")
        self.assertEqual(self.client.get(TODOS_URL).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.put(token="not-a-token").status_code, status.HTTP_401_UNAUTHORIZED)

    def test_a_removed_pc(self) -> None:
        Device.objects.filter(owner=self.owner).update(is_revoked=True)
        self.assertEqual(self.put().status_code, status.HTTP_401_UNAUTHORIZED)

    def test_a_pc_without_a_member(self) -> None:
        Device.objects.filter(owner=self.owner).update(member=None)
        self.as_device(self.token)
        self.assertEqual(self.client.get(TODOS_URL).json(), {"member_id": None, "todos": []})
        response = self.put(client_id="aaaaaaaa11111111")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.json()["code"], "no_member")
        self.assertEqual(self.delete(client_id="aaaaaaaa11111111").status_code, status.HTTP_409_CONFLICT)


class TodoPlanGateTests(PlanTestCase):
    """To-dos ride on team_chat, like tasks. Without it, every endpoint says 403 and the Windows app stays local."""

    def setUp(self) -> None:
        super().setUp()
        self.token = self.enroll().json()["device_token"]
        self.use_plan("startups")  # no team_chat in the split

    def test_device_endpoints_locked(self) -> None:
        self.as_device(self.token)
        at = stamp().isoformat()
        for response in (
            self.client.get(TODOS_URL),
            self.client.put(todo_url(), {"title": "x", "updated_at": at}, format="json"),
            self.client.delete(todo_url(), {"updated_at": at}, format="json"),
        ):
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
            self.assertEqual(response.json()["module"], "team_chat")
        self.assertFalse(MemberTodo.objects.exists())

    def test_owner_endpoints_locked(self) -> None:
        self.as_owner()
        for url in (TODOS_URL, f"{MEMBERS_URL}{self.member.id}/todos/"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
            self.assertEqual(response.json()["module"], "team_chat")
