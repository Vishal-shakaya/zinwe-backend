"""The owner's Activity timeline: work-day changes logged from heartbeats (GET work-log/)."""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from rest_framework import status

from z_auth._testing import give_plan

from .models import WorkLog
from .tests import DeviceTestCase

HEARTBEAT_URL = "/api/devices/heartbeat/"
WORK_LOG_URL = "/api/devices/work-log/"


class WorkLogTests(DeviceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.token = self.enroll().json()["device_token"]

    def beat(self, state: str, **extra):  # type: ignore[no-untyped-def]
        self.as_device(self.token)
        return self.client.post(HEARTBEAT_URL, {"work_state": state, **extra}, format="json")

    def _events(self, start, *steps):  # type: ignore[no-untyped-def]
        """[(seconds after start, state, reason, note), ...] -> the agent's work_events list."""
        return [
            {"at": (start + timedelta(seconds=s)).isoformat(), "state": st, "reason": r, "note": n}
            for s, st, r, n in steps
        ]

    def test_every_change_is_logged_even_within_one_heartbeat(self) -> None:
        from .models import WorkLog

        start = timezone.now() - timedelta(minutes=5)
        events = self._events(
            start,
            (0, "working", "", ""),
            (20, "on_break", "Lunch", "quick bite"),
            (40, "working", "", ""),
            (60, "ended", "Leaving early", "Doctor"),
        )
        self.beat("ended", work_events=events)

        rows = list(WorkLog.objects.order_by("at").values_list("state", "previous_state", "reason", "note", "approval"))
        self.assertEqual(rows, [
            ("working", "", "", "", ""),
            ("on_break", "working", "Lunch", "quick bite", "pending"),
            ("working", "on_break", "", "", ""),               # "back from break", not a new log in
            ("ended", "working", "Leaving early", "Doctor", "pending"),  # FR-16: log-outs wait for an OK too
        ])

        # A retry (lost response) re-sends the same changes: nothing is added twice.
        self.beat("ended", work_events=events)
        self.assertEqual(WorkLog.objects.count(), 4)

    def test_a_change_the_older_path_already_logged_is_not_added_again(self) -> None:
        from .models import WorkLog

        # The state-diff path (older agent / first heartbeat after an upgrade) logged a log-out...
        ended_at = timezone.now() - timedelta(minutes=2)
        self.beat("working")
        self.beat("ended", work_changed_at=ended_at.isoformat(), end_reason="Not feeling well")
        # ...then the new agent sends the same press from the change log, a few microseconds apart.
        self.beat("ended", work_events=self._events(ended_at - timedelta(microseconds=37), (0, "ended", "", "")))

        ended = WorkLog.objects.filter(state="ended")
        self.assertEqual(ended.count(), 1)
        self.assertEqual(ended.get().reason, "Not feeling well")

    def test_changes_made_offline_arrive_with_the_next_heartbeat(self) -> None:
        from .models import WorkLog

        self.beat("working", work_events=self._events(timezone.now() - timedelta(hours=3), (0, "working", "", "")))
        offline = timezone.now() - timedelta(hours=2)
        self.beat("working", work_events=self._events(
            offline, (0, "on_break", "Meeting", ""), (900, "working", "", "")))
        self.assertEqual(
            list(WorkLog.objects.order_by("at").values_list("state", flat=True)),
            ["working", "on_break", "working"],
        )

    def test_events_too_old_or_in_the_future_are_ignored(self) -> None:
        from .models import WorkLog

        now = timezone.now()
        self.beat("working", work_events=self._events(now - timedelta(days=8), (0, "working", "", "")))
        self.beat("working", work_events=self._events(now + timedelta(hours=1), (0, "working", "", "")))
        self.assertEqual(WorkLog.objects.count(), 0)

    def test_an_empty_list_means_nothing_changed(self) -> None:
        from .models import WorkLog

        # A newer agent with no new changes must not fall back to the "state differs" guess.
        self.beat("working", work_events=[])
        self.assertEqual(WorkLog.objects.count(), 0)

    def test_log_out_keeps_its_reason_and_note(self) -> None:
        from .models import WorkLog

        self.beat("working")
        self.beat("ended", end_reason="Leaving early", end_note="Doctor's appointment",
                  break_reason="Lunch", break_note="stale break fields are ignored")
        ended = WorkLog.objects.get(state="ended")
        self.assertEqual((ended.reason, ended.note, ended.approval), ("Leaving early", "Doctor's appointment", "pending"))
        # Logging in carries no reason, even if an agent sends some.
        self.beat("working", end_reason="Leaving early")
        self.assertEqual(WorkLog.objects.filter(state="working").last().reason, "")

    def test_day_is_logged_from_state_changes(self) -> None:
        # Earlier today, but never before local midnight (the owner's view is per local day).
        now = timezone.now()
        midnight = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
        started = max(now - timedelta(hours=3), midnight + timedelta(seconds=1))
        self.beat("working", work_changed_at=started.isoformat())
        self.beat("working")  # no change: nothing logged
        self.beat("on_break", break_reason="Lunch")
        self.beat("working")
        self.beat("ended")

        logs = list(WorkLog.objects.all())
        self.assertEqual([l.state for l in logs], ["working", "on_break", "working", "ended"])
        self.assertLess(abs((logs[0].at - started).total_seconds()), 1)  # widget's time trusted
        self.assertEqual(logs[1].reason, "Lunch")
        self.assertEqual(logs[2].reason, "")  # only breaks carry a reason
        self.assertEqual(logs[1].member_id, self.member.id)

        self.as_owner()
        body = self.client.get(WORK_LOG_URL).json()
        self.assertEqual([e["state"] for e in body["entries"]], ["working", "on_break", "working", "ended"])
        self.assertEqual(body["entries"][1]["member_name"], "Asha Rao")
        self.assertEqual(body["entries"][1]["reason"], "Lunch")

    def test_implausible_widget_time_falls_back_to_now(self) -> None:
        self.beat("working", work_changed_at=(timezone.now() + timedelta(hours=2)).isoformat())
        self.assertLess(abs((WorkLog.objects.get().at - timezone.now()).total_seconds()), 5)

    def test_not_started_is_not_logged(self) -> None:
        self.beat("not_started")
        self.assertFalse(WorkLog.objects.exists())

    def test_old_agent_without_new_fields(self) -> None:
        response = self.beat("on_break")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(WorkLog.objects.get().reason, "")

    def test_other_days_and_other_owners_are_excluded(self) -> None:
        self.beat("working")
        WorkLog.objects.update(at=timezone.now() - timedelta(days=2))
        self.as_owner()
        self.assertEqual(self.client.get(WORK_LOG_URL).json()["entries"], [])
        day = (timezone.localdate() - timedelta(days=2)).isoformat()
        self.assertEqual(len(self.client.get(WORK_LOG_URL, {"date": day}).json()["entries"]), 1)

        from z_auth.models import User
        from rest_framework.authtoken.models import Token

        other = User.objects.create_user("other@else.in", "pw-12345678")
        give_plan(other, "businesses")  # same plan, so only ownership decides what they see
        token, _ = Token.objects.get_or_create(user=other)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        self.assertEqual(self.client.get(WORK_LOG_URL, {"date": day}).json()["entries"], [])


class BreakDecisionTests(DeviceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.token = self.enroll().json()["device_token"]
        self.as_device(self.token)
        self.client.post(HEARTBEAT_URL, {"work_state": "working"}, format="json")
        self.client.post(
            HEARTBEAT_URL, {"work_state": "on_break", "break_reason": "Lunch", "break_note": "Bank errand too"},
            format="json",
        )
        self.brk = WorkLog.objects.get(state="on_break")

    def decide(self, log_id: int, decision: str, note: str = ""):  # type: ignore[no-untyped-def]
        self.as_owner()
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(f"{WORK_LOG_URL}{log_id}/decision/", {"decision": decision, "note": note},
                                    format="json")

    def test_break_is_pending_with_note(self) -> None:
        self.assertEqual((self.brk.approval, self.brk.note), ("pending", "Bank errand too"))

    def test_deny_tells_the_member(self) -> None:
        from .models import Message

        response = self.decide(self.brk.id, "denied", "Please take errands after work.")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertEqual(response.json()["approval"], "denied")
        message = Message.objects.get()
        # Kind wording (J6-18): "About your Lunch break at …", an info message, the owner's note as the body.
        self.assertTrue(message.title.startswith("About your Lunch break at"))
        self.assertEqual(message.body, "Please take errands after work.")
        self.assertEqual(message.severity, "info")

    def test_deny_without_a_note_invites_a_reply(self) -> None:
        from .models import Message

        self.decide(self.brk.id, "denied", "")
        self.assertIn("would like a word about this break. You can reply here.", Message.objects.get().body)

    def test_only_breaks(self) -> None:
        working = WorkLog.objects.get(state="working")
        self.assertEqual(self.decide(working.id, "approved").status_code, status.HTTP_400_BAD_REQUEST)

    def test_other_owner_gets_404(self) -> None:
        from rest_framework.authtoken.models import Token
        from z_auth.models import User

        other = User.objects.create_user("other@else.in", "pw-12345678")
        give_plan(other, "businesses")  # same plan, so only ownership decides the answer
        token, _ = Token.objects.get_or_create(user=other)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        response = self.client.post(f"{WORK_LOG_URL}{self.brk.id}/decision/", {"decision": "approved"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class SeedDemoActivityTests(DeviceTestCase):
    def test_seed_and_clear(self) -> None:
        from io import StringIO

        from django.core.management import call_command
        from django.test import override_settings

        from z_auth.models import Company

        from .models import Device, Member

        Company.objects.create(owner=self.owner, name="Interior bazzar", industry="retail", team_size="11-25",
                               phone="+91 90000 00000", city="Delhi", state="Delhi")
        with override_settings(DEBUG=True):
            call_command("seed_demo_activity", "--days", "3", stdout=StringIO())
            self.assertEqual(Member.objects.filter(email__endswith="@demo.zinwe.local").count(), 3)
            self.assertTrue(WorkLog.objects.filter(state="on_break").exclude(reason="").exists())
            # Re-running replaces the demo data instead of piling up.
            first = WorkLog.objects.count()
            call_command("seed_demo_activity", "--days", "3", stdout=StringIO())
            self.assertEqual(WorkLog.objects.count(), first)
            call_command("seed_demo_activity", "--clear", stdout=StringIO())
        self.assertEqual(Member.objects.filter(email__endswith="@demo.zinwe.local").count(), 0)
        self.assertFalse(Device.objects.filter(machine_id__startswith="demo-").exists())
        self.assertTrue(Member.objects.filter(pk=self.member.pk).exists())  # real member untouched


class ConsentHeartbeatTests(DeviceTestCase):
    def test_heartbeat_records_member_consent(self) -> None:
        from .models import Device

        token = self.enroll().json()["device_token"]
        self.as_device(token)
        at = (timezone.now() - timedelta(minutes=5)).isoformat()
        self.client.post(HEARTBEAT_URL, {"work_state": "not_started", "consent_version": "2026-10-14", "consent_at": at},
                         format="json")
        device = Device.objects.get()
        self.assertEqual(device.consent_version, "2026-10-14")
        self.assertIsNotNone(device.consent_at)
        # An old agent that doesn't send it never wipes it.
        self.client.post(HEARTBEAT_URL, {"work_state": "not_started"}, format="json")
        device.refresh_from_db()
        self.assertEqual(device.consent_version, "2026-10-14")
        self.as_owner()
        member = self.client.get("/api/devices/members/").json()[0]
        self.assertEqual(member["device"]["consent_version"], "2026-10-14")


class LiveWorkTimeTests(DeviceTestCase):
    """FR-03: the widget reports settled seconds only; the dashboard adds the stretch still running."""

    def setUp(self) -> None:
        super().setUp()
        self.token = self.enroll().json()["device_token"]

    def beat(self, state: str, **extra):  # type: ignore[no-untyped-def]
        self.as_device(self.token)
        return self.client.post(HEARTBEAT_URL, {"work_state": state, **extra}, format="json")

    def _member_device(self):  # type: ignore[no-untyped-def]
        self.as_owner()
        return self.client.get("/api/devices/members/").json()[0]["device"]

    def test_working_since_morning_with_no_break_counts_the_running_stretch(self) -> None:
        since = (timezone.now() - timedelta(hours=2)).isoformat()
        self.beat("working", worked_seconds=0, break_seconds=0, work_changed_at=since)
        device = self._member_device()
        self.assertGreaterEqual(device["worked_seconds"], 2 * 3600 - 5)
        self.assertLessEqual(device["worked_seconds"], 2 * 3600 + 60)
        self.assertEqual(device["break_seconds"], 0)

    def test_after_a_break_the_day_total_keeps_growing(self) -> None:
        self.beat("on_break", worked_seconds=3 * 3600, break_seconds=0,
                  work_changed_at=(timezone.now() - timedelta(minutes=10)).isoformat())
        device = self._member_device()
        self.assertEqual(device["worked_seconds"], 3 * 3600)  # paused during the break
        self.assertGreaterEqual(device["break_seconds"], 600 - 5)
        self.beat("working", worked_seconds=3 * 3600, break_seconds=900,
                  work_changed_at=(timezone.now() - timedelta(minutes=30)).isoformat())
        device = self._member_device()
        self.assertGreaterEqual(device["worked_seconds"], 3 * 3600 + 1800 - 5)
        self.assertEqual(device["break_seconds"], 900)

    def test_a_pc_switched_off_stops_counting_at_its_last_heartbeat(self) -> None:
        from .models import Device

        self.beat("working", worked_seconds=0, break_seconds=0,
                  work_changed_at=(timezone.now() - timedelta(hours=3)).isoformat())
        Device.objects.update(last_seen_at=timezone.now() - timedelta(hours=1))
        device = self._member_device()
        self.assertGreaterEqual(device["worked_seconds"], 2 * 3600 - 5)
        self.assertLessEqual(device["worked_seconds"], 2 * 3600 + 5)

    def test_ended_day_reports_the_settled_totals(self) -> None:
        self.beat("ended", worked_seconds=7 * 3600, break_seconds=1800,
                  work_changed_at=(timezone.now() - timedelta(hours=1)).isoformat())
        device = self._member_device()
        self.assertEqual((device["worked_seconds"], device["break_seconds"]), (7 * 3600, 1800))


class LogOutApprovalTests(DeviceTestCase):  # FR-16
    """A log-out is approved or questioned like a break, after the fact; the member hears in their widget."""

    def setUp(self) -> None:
        super().setUp()
        self.token = self.enroll().json()["device_token"]

    def test_owner_approves_or_questions_a_log_out(self) -> None:
        from .models import Message, WorkLog

        self.as_device(self.token)
        self.client.post("/api/devices/heartbeat/", {"work_state": "working"}, format="json")
        self.client.post("/api/devices/heartbeat/", {"work_state": "ended", "end_reason": "Leaving early"}, format="json")
        ended = WorkLog.objects.get(state="ended")
        self.assertEqual(ended.approval, "pending")

        self.as_owner()
        url = f"/api/devices/work-log/{ended.pk}/decision/"
        response = self.client.post(url, {"decision": "denied", "note": ""}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        msg = Message.objects.filter(member=self.member).latest("created_at")
        self.assertTrue(msg.title.startswith("About your log-out at "))
        self.assertIn("would like a word about this log-out", msg.body)

        self.client.post(url, {"decision": "approved", "note": "Feel better"}, format="json")
        msg = Message.objects.filter(member=self.member).latest("created_at")
        self.assertTrue(msg.title.startswith("Your log-out at ") and msg.title.endswith("was approved"))
        self.assertEqual(msg.body, "Feel better")

    def test_logging_in_is_not_something_to_approve(self) -> None:
        from .models import WorkLog

        self.as_device(self.token)
        self.client.post("/api/devices/heartbeat/", {"work_state": "working"}, format="json")
        start = WorkLog.objects.get(state="working")
        self.as_owner()
        response = self.client.post(f"/api/devices/work-log/{start.pk}/decision/", {"decision": "approved"}, format="json")
        self.assertEqual(response.status_code, 400)
