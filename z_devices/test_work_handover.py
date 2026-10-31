"""
One person, one work day, across PCs: the server decides which of a member's PCs owns the day (last Start day wins).
The old PC is told where the day went, the new PC what to carry on from, and every total counts the day once.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone
from rest_framework import status

from z_auth.models import Plan

from .models import Device, Member, WorkLog
from .reports import build_report
from .services import current_device_for, record_heartbeat, work_day_totals
from .tests import DeviceTestCase
from .work_time import day_start, local_day, work_zone

HEARTBEAT = "/api/devices/heartbeat/"
WORK_LOG = "/api/devices/work-log/"
H = 3600


class HandoverTestCase(DeviceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.pc1 = self.enroll(hostname="OFFICE-PC", machine_id="m1").json()["device_token"]
        self.pc2 = self.enroll(hostname="LAPTOP-X", machine_id="m2").json()["device_token"]
        self.d1 = Device.objects.get(machine_id="m1")
        self.d2 = Device.objects.get(machine_id="m2")
        # The owner's work zone, as the code counts a day (never a date read off an aware datetime).
        self.tz = work_zone(self.owner)
        self.midnight = day_start(local_day(timezone.now(), self.tz), self.tz)

    def beat(self, token: str, state: str = "not_started", **extra: Any):  # type: ignore[no-untyped-def]
        self.as_device(token)
        if isinstance(extra.get("work_changed_at"), datetime):
            extra["work_changed_at"] = extra["work_changed_at"].isoformat()
        response = self.client.post(HEARTBEAT, {"work_state": state, **extra}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        return response.json()

    def press(self, token: str, state: str, at: datetime, **extra: Any):  # type: ignore[no-untyped-def]
        """A heartbeat right after a button press: the change with its exact time, as a newer agent sends it."""
        event = {"at": at.isoformat(), "state": state, "reason": extra.pop("reason", ""), "note": ""}
        return self.beat(token, state, work_changed_at=at, work_events=[event], **extra)

    def shift(self) -> tuple[datetime, datetime]:
        """
        A start, and Start day on the other PC up to an hour later: both today, both already in the past. Whole seconds
        and a span that divides evenly: stretches are counted in whole seconds, so a stray fraction would come up short.
        """
        late = max(timezone.now().replace(microsecond=0) - timedelta(minutes=1), self.midnight + timedelta(minutes=2))
        room = int((late - self.midnight).total_seconds()) - 60
        return late - timedelta(seconds=min(3600, room - room % 12)), late

    def totals(self, now: datetime) -> dict[str, Any]:
        rows = work_day_totals(self.owner, local_day(now, self.tz), now=now)["totals"]
        self.assertEqual(len(rows), 1, "one person, one row")
        return rows[0]


class TwoPcsTests(HandoverTestCase):
    def test_start_on_a_second_pc_takes_the_day_over(self) -> None:
        early, late = self.shift()
        self.press(self.pc1, "working", early)
        reply = self.press(self.pc2, "working", late)

        carry = reply["day_carry"]
        self.assertEqual(carry["worked_seconds"], int((late - early).total_seconds()))
        self.assertEqual((carry["break_seconds"], carry["breaks"], carry["from_hostname"]), (0, 0, "OFFICE-PC"))
        self.assertEqual(carry["first_start"], early.isoformat())
        self.assertNotIn("day_moved", reply)

        # The old PC hears where the day went, at the same moment, on its next heartbeat.
        moved = self.beat(self.pc1, "working", work_changed_at=early, work_events=[])
        self.assertEqual(moved["day_moved"]["to_hostname"], "LAPTOP-X")
        self.assertEqual(datetime.fromisoformat(moved["day_moved"]["at"].replace("Z", "+00:00")), late)
        self.assertNotIn("day_carry", moved)

    def test_moving_is_not_a_log_out_and_asks_nobody_for_anything(self) -> None:
        early, late = self.shift()
        self.press(self.pc1, "working", early)
        self.press(self.pc2, "working", late)
        self.beat(self.pc1, "working", work_changed_at=early, work_events=[])

        self.assertFalse(WorkLog.objects.filter(state="ended").exists())
        self.assertFalse(WorkLog.objects.exclude(approval="").exists())
        line = WorkLog.objects.get(device=self.d2)
        self.assertEqual((line.state, line.moved_from, line.reason, line.previous_state),
                         ("working", "OFFICE-PC", "", "working"))

    def test_the_first_pc_stays_enrolled_and_connected(self) -> None:
        early, late = self.shift()
        self.press(self.pc1, "working", early)
        self.press(self.pc2, "working", late)
        self.beat(self.pc1, "working", work_changed_at=early, work_events=[])

        self.d1.refresh_from_db()
        self.assertFalse(self.d1.is_revoked)
        self.assertTrue(self.d1.is_online)
        self.assertEqual(self.d1.member_id, self.member.id)

    def test_the_seconds_both_pcs_overlap_count_once(self) -> None:
        early, late = self.shift()
        self.press(self.pc1, "working", early)
        self.press(self.pc2, "working", late)
        # PC 1 hasn't heard yet and says it's still working; its next heartbeats change nothing.
        for _ in range(3):
            self.beat(self.pc1, "working", worked_seconds=0, work_changed_at=early, work_events=[])

        self.d1.refresh_from_db()
        self.assertEqual((self.d1.work_state, self.d1.live_work_seconds()), ("not_started", (0, 0)))
        now = timezone.now()
        self.assertEqual(self.totals(now)["worked_seconds"], int((now - early).total_seconds()))
        self.as_owner()
        body = self.client.get(WORK_LOG).json()
        self.assertEqual(len(body["totals"]), 1)
        self.assertEqual(len({e["member_id"] for e in body["entries"]}), 1)

    def test_a_report_counts_the_day_once(self) -> None:
        now = timezone.now()
        day = local_day(now, self.tz)
        early, late = self.shift()
        self.press(self.pc1, "working", early)
        self.press(self.pc2, "working", late)
        self.beat(self.pc1, "working", work_changed_at=early, work_events=[])
        report = build_report(self.owner, day, day, now=now + timedelta(seconds=5))
        person = report["people"][0]
        self.assertEqual(len(report["people"]), 1)
        self.assertEqual(person["days_worked"], 1)
        self.assertEqual(person["logouts"], 0)
        self.assertEqual(person["worked_seconds"], int((now + timedelta(seconds=5) - early).total_seconds()))

    def test_breaks_before_the_move_are_carried(self) -> None:
        early, late = self.shift()
        gap = (late - early) / 4
        self.press(self.pc1, "working", early)
        self.press(self.pc1, "on_break", early + gap, reason="Lunch")
        self.press(self.pc1, "working", early + 2 * gap)
        carry = self.press(self.pc2, "working", late)["day_carry"]
        self.assertEqual(carry["breaks"], 1)
        self.assertEqual(carry["break_seconds"], int(gap.total_seconds()))
        self.assertEqual(carry["worked_seconds"], int(3 * gap.total_seconds()))

    def test_moving_during_a_break_closes_the_break(self) -> None:
        early, late = self.shift()
        self.press(self.pc1, "working", early)
        brk = early + (late - early) / 2
        self.press(self.pc1, "on_break", brk, reason="Lunch")
        carry = self.press(self.pc2, "working", late)["day_carry"]
        self.assertEqual((carry["breaks"], carry["break_seconds"]), (1, int((late - brk).total_seconds())))
        self.assertEqual(self.totals(timezone.now())["breaks"], 1)

    def test_the_dashboard_shows_where_the_day_is_and_a_full_day_so_far(self) -> None:
        early, late = self.shift()
        self.press(self.pc1, "working", early)
        self.press(self.pc2, "working", late)
        # PC 1 heartbeats after PC 2, so it's "most recently seen"; the day is still on PC 2.
        self.beat(self.pc1, "not_started")
        current = current_device_for(self.member)
        self.assertEqual(current.hostname, "LAPTOP-X")
        self.assertGreaterEqual(current.live_work_seconds()[0], int((late - early).total_seconds()))

    def test_the_line_is_on_the_activity_feed(self) -> None:
        early, late = self.shift()
        self.press(self.pc1, "working", early)
        self.press(self.pc2, "working", late)
        self.as_owner()
        lines = self.client.get(WORK_LOG).json()["entries"]
        self.assertEqual([(e["hostname"], e["moved_from"]) for e in lines],
                         [("OFFICE-PC", ""), ("LAPTOP-X", "OFFICE-PC")])

    def test_the_carry_stays_on_the_response_until_the_timer_has_it(self) -> None:
        early, late = self.shift()
        self.press(self.pc1, "working", early)
        first = self.press(self.pc2, "working", late)["day_carry"]
        # The response was lost, or the agent restarted: the next heartbeat says the same.
        again = self.beat(self.pc2, "working", work_changed_at=late, work_events=[])["day_carry"]
        self.assertEqual(first, again)

    def test_a_timer_that_has_not_taken_the_carry_yet_does_not_shrink_the_day(self) -> None:
        early, late = self.shift()
        self.press(self.pc1, "working", early)
        carried = self.press(self.pc2, "working", late)["day_carry"]["worked_seconds"]
        self.beat(self.pc2, "working", worked_seconds=0, work_changed_at=late, work_events=[])
        self.d2.refresh_from_db()
        self.assertEqual(self.d2.worked_seconds, carried)
        self.beat(self.pc2, "working", worked_seconds=carried + 60, work_changed_at=late, work_events=[])
        self.d2.refresh_from_db()
        self.assertEqual(self.d2.worked_seconds, carried + 60)


class MovingBackTests(HandoverTestCase):
    def three_times(self) -> tuple[datetime, datetime, datetime]:
        early, late = self.shift()
        step = (late - early) / 3
        return early, early + step, early + 2 * step

    def test_start_on_the_first_pc_takes_it_back_the_same_way(self) -> None:
        t1, t2, t3 = self.three_times()
        self.press(self.pc1, "working", t1)
        self.press(self.pc2, "working", t2)
        self.beat(self.pc1, "not_started")  # it heard, and its timer stopped

        reply = self.press(self.pc1, "working", t3)
        carry = reply["day_carry"]
        self.assertEqual(carry["worked_seconds"], int((t3 - t1).total_seconds()))
        self.assertEqual(carry["from_hostname"], "LAPTOP-X")
        self.assertNotIn("day_moved", reply)

        told = self.beat(self.pc2, "working", work_changed_at=t2, work_events=[])
        self.assertEqual(told["day_moved"]["to_hostname"], "OFFICE-PC")
        self.d1.refresh_from_db()
        self.d2.refresh_from_db()
        self.assertEqual((self.d1.work_state, self.d2.work_state), ("working", "not_started"))
        self.assertEqual(
            list(WorkLog.objects.order_by("at").values_list("device__hostname", "moved_from")),
            [("OFFICE-PC", ""), ("LAPTOP-X", "OFFICE-PC"), ("OFFICE-PC", "LAPTOP-X")],
        )
        now = timezone.now()
        self.assertEqual(self.totals(now)["worked_seconds"], int((now - t1).total_seconds()))

    def test_a_third_pc_points_the_first_one_at_where_the_day_is(self) -> None:
        t1, t2, t3 = self.three_times()
        pc3 = self.enroll(hostname="HOME-PC", machine_id="m3").json()["device_token"]
        self.press(self.pc1, "working", t1)
        self.press(self.pc2, "working", t2)
        self.press(pc3, "working", t3)
        self.assertEqual(self.beat(self.pc1, "not_started")["day_moved"]["to_hostname"], "HOME-PC")
        self.assertEqual(self.beat(self.pc2, "not_started")["day_moved"]["to_hostname"], "HOME-PC")

    def test_start_after_the_day_ended_elsewhere_is_no_move_but_the_timer_carries_on(self) -> None:
        t1, t2, t3 = self.three_times()
        self.press(self.pc1, "working", t1)
        self.press(self.pc2, "working", t2)
        self.press(self.pc2, "ended", t2 + timedelta(seconds=30), reason="Done for today")
        self.beat(self.pc1, "not_started")

        carry = self.press(self.pc1, "working", t3)["day_carry"]
        self.assertEqual(carry["worked_seconds"], int((t2 + timedelta(seconds=30) - t1).total_seconds()))
        self.assertEqual(carry["from_hostname"], "")
        self.assertEqual(WorkLog.objects.exclude(moved_from="").count(), 1)  # only the first move


class OfflineTests(HandoverTestCase):
    def test_a_pc_offline_during_the_move_hears_when_it_returns(self) -> None:
        early, late = self.shift()
        self.press(self.pc1, "working", early)
        self.press(self.pc2, "working", late)
        reply = self.beat(self.pc1, "working", worked_seconds=0, work_changed_at=early, work_events=[])
        self.assertEqual(reply["day_moved"]["to_hostname"], "LAPTOP-X")

    def test_a_pc_that_went_quiet_left_off_at_its_last_heartbeat(self) -> None:
        # PC 1 was switched off an hour before Start day on PC 2: that hour isn't work time.
        early, late = self.shift()
        self.press(self.pc1, "working", early)
        seen = early + timedelta(seconds=30)
        Device.objects.filter(pk=self.d1.pk).update(last_seen_at=seen)
        if late - seen <= timedelta(minutes=3):
            self.skipTest("too close to midnight for an hour-long gap")

        carry = self.press(self.pc2, "working", late)["day_carry"]
        self.assertEqual(carry["worked_seconds"], 30)
        self.assertEqual(WorkLog.objects.get(device=self.d2).left_off_at, seen)
        now = timezone.now()
        self.assertEqual(self.totals(now)["worked_seconds"], 30 + int((now - late).total_seconds()))

    def test_a_short_gap_in_heartbeats_is_not_quiet(self) -> None:
        early, late = self.shift()
        self.press(self.pc1, "working", early)
        Device.objects.filter(pk=self.d1.pk).update(last_seen_at=late - timedelta(seconds=90))
        carry = self.press(self.pc2, "working", late)["day_carry"]
        self.assertEqual(carry["worked_seconds"], int((late - early).total_seconds()))
        self.assertIsNone(WorkLog.objects.get(device=self.d2).left_off_at)

    def test_what_it_did_before_the_move_still_arrives_and_what_came_after_does_not(self) -> None:
        early, late = self.shift()
        step = (late - early) / 4
        self.press(self.pc1, "working", early)
        self.press(self.pc2, "working", late)
        # Back online with changes made offline: a break before the move, a break after it.
        events = [
            {"at": (early + step).isoformat(), "state": "on_break", "reason": "Lunch", "note": ""},
            {"at": (early + 2 * step).isoformat(), "state": "working", "reason": "", "note": ""},
            {"at": (late + timedelta(seconds=5)).isoformat(), "state": "on_break", "reason": "Tea", "note": ""},
        ]
        self.beat(self.pc1, "on_break", work_changed_at=late + timedelta(seconds=5), work_events=events)
        reasons = list(WorkLog.objects.filter(state="on_break").values_list("reason", flat=True))
        self.assertEqual(reasons, ["Lunch"])

    def test_start_pressed_offline_moves_the_day_when_the_pc_reconnects(self) -> None:
        early, late = self.shift()
        pressed = early + (late - early) / 2
        self.press(self.pc1, "working", early)
        self.beat(self.pc1, "working", work_changed_at=early, work_events=[])
        carry = self.press(self.pc2, "working", pressed)["day_carry"]  # arrives later, with its real time
        self.assertEqual(carry["worked_seconds"], int((pressed - early).total_seconds()))
        self.assertEqual(self.beat(self.pc1, "working", work_changed_at=early, work_events=[])["day_moved"]["to_hostname"],
                         "LAPTOP-X")

    def test_a_start_older_than_the_other_pcs_loses_last_start_wins(self) -> None:
        early, late = self.shift()
        self.press(self.pc1, "working", late)
        reply = self.press(self.pc2, "working", early)  # pressed earlier, offline, arrives after
        self.assertEqual(reply["day_moved"]["to_hostname"], "OFFICE-PC")
        self.d2.refresh_from_db()
        self.assertEqual(self.d2.work_state, "not_started")
        # PC 2's stretch is part of the day, and PC 1 picks it up.
        carry = self.beat(self.pc1, "working", work_changed_at=late, work_events=[])["day_carry"]
        self.assertEqual(carry["worked_seconds"], int((late - early).total_seconds()))
        self.assertEqual(WorkLog.objects.get(device=self.d1).moved_from, "LAPTOP-X")


class OlderAgentTests(HandoverTestCase):
    def test_an_older_agent_that_never_stops_is_not_counted(self) -> None:
        early, late = self.shift()
        self.beat(self.pc1, "working", worked_seconds=0, work_changed_at=early)  # no work_events: older than those
        self.press(self.pc2, "working", late)
        for _ in range(3):
            self.beat(self.pc1, "working", worked_seconds=7200, work_changed_at=early)

        self.d1.refresh_from_db()
        self.assertEqual((self.d1.work_state, self.d1.worked_seconds), ("not_started", 0))
        self.assertEqual(WorkLog.objects.filter(device=self.d1).count(), 1)
        self.assertEqual(self.d1.live_work_seconds(), (0, 0))

    def test_a_break_or_end_on_an_older_agent_after_the_move_changes_nothing(self) -> None:
        early, late = self.shift()
        self.press(self.pc1, "working", early)
        self.press(self.pc2, "working", late)
        later = late + timedelta(seconds=5)
        self.beat(self.pc1, "on_break", break_reason="Lunch", work_changed_at=later)
        self.beat(self.pc1, "ended", end_reason="Done for today", work_changed_at=later)
        self.assertEqual(WorkLog.objects.count(), 2)
        self.d2.refresh_from_db()
        self.assertEqual(self.d2.work_state, "working")

    def test_an_older_agent_on_the_new_pc_still_takes_over(self) -> None:
        early, late = self.shift()
        self.beat(self.pc1, "working", work_changed_at=early)
        self.beat(self.pc2, "working", work_changed_at=late)
        self.assertEqual(WorkLog.objects.get(device=self.d2).moved_from, "OFFICE-PC")
        self.d2.refresh_from_db()
        self.assertEqual(self.d2.worked_seconds, int((late - early).total_seconds()))

    def test_an_older_agent_pressing_start_again_takes_the_day_back(self) -> None:
        early, late = self.shift()
        self.beat(self.pc1, "working", work_changed_at=early)
        self.beat(self.pc2, "working", work_changed_at=late - timedelta(seconds=20))
        self.beat(self.pc1, "working", work_changed_at=late)
        self.d1.refresh_from_db()
        self.d2.refresh_from_db()
        self.assertEqual((self.d1.work_state, self.d2.work_state), ("working", "not_started"))

    def test_an_older_agent_without_a_time_stays_superseded(self) -> None:
        early, late = self.shift()
        self.beat(self.pc1, "working", work_changed_at=early)
        self.press(self.pc2, "working", late)
        self.beat(self.pc1, "working")
        self.d1.refresh_from_db()
        self.assertEqual(self.d1.work_state, "not_started")


class RaceTests(HandoverTestCase):
    def data(self, state: str, at: datetime) -> dict[str, Any]:
        return {"work_state": state, "worked_seconds": 0, "break_seconds": 0, "work_changed_at": at,
                "work_events": [{"at": at, "state": state, "reason": "", "note": ""}]}

    def test_a_heartbeat_read_before_the_other_pcs_commit_sees_it_under_the_lock(self) -> None:
        early, late = self.shift()
        record_heartbeat(Device.objects.get(pk=self.d1.pk), self.data("working", early), None)
        # PC 1's request loaded its row, then waited for the lock while PC 2 took the day.
        stale = Device.objects.get(pk=self.d1.pk)
        record_heartbeat(Device.objects.get(pk=self.d2.pk), self.data("working", late), None)
        reply = record_heartbeat(stale, {**self.data("working", early), "work_events": []}, None)
        self.assertEqual(reply["day_moved"]["to_hostname"], "LAPTOP-X")
        self.d1.refresh_from_db()
        self.assertEqual((self.d1.work_state, self.d1.work_moved_to_id), ("not_started", self.d2.id))

    def test_two_starts_at_once_leave_one_owner_whichever_arrives_first(self) -> None:
        early, late = self.shift()
        record_heartbeat(Device.objects.get(pk=self.d2.pk), self.data("working", late), None)
        record_heartbeat(Device.objects.get(pk=self.d1.pk), self.data("working", early), None)
        active = Device.objects.filter(work_state__in=["working", "on_break"])
        self.assertEqual([d.pk for d in active], [self.d2.pk])

    def test_the_same_second_goes_to_the_pc_reporting_now(self) -> None:
        _, late = self.shift()
        record_heartbeat(Device.objects.get(pk=self.d1.pk), self.data("working", late), None)
        record_heartbeat(Device.objects.get(pk=self.d2.pk), self.data("working", late), None)
        self.assertEqual([d.pk for d in Device.objects.filter(work_state="working")], [self.d2.pk])

    def test_the_lock_is_taken_inside_the_heartbeats_own_transaction(self) -> None:
        # select_for_update outside a transaction raises on PostgreSQL, and SQLite would never show it.
        from unittest import mock

        from django.db import connection

        from . import services

        depth = []
        real = services._settle_work_day

        def spy(*args: Any) -> bool:
            depth.append(len(connection.savepoint_ids))
            return real(*args)

        early, _ = self.shift()
        before = len(connection.savepoint_ids)
        with mock.patch.object(services, "_settle_work_day", spy):
            record_heartbeat(Device.objects.get(pk=self.d1.pk), self.data("working", early), None)
        self.assertEqual(depth, [before + 1])


class NobodyElseTests(HandoverTestCase):
    def test_a_member_with_one_pc_is_unaffected(self) -> None:
        Device.objects.filter(pk=self.d2.pk).delete()
        early, late = self.shift()
        reply = self.press(self.pc1, "working", early)
        self.assertNotIn("day_moved", reply)
        self.assertNotIn("day_carry", reply)
        self.press(self.pc1, "ended", late, reason="Done for today")
        self.press(self.pc1, "working", late + timedelta(seconds=10))
        rows = list(WorkLog.objects.order_by("at").values_list("state", "moved_from", "left_off_at"))
        self.assertEqual(rows, [("working", "", None), ("ended", "", None), ("working", "", None)])

    def test_different_members_on_their_own_pcs_are_unaffected(self) -> None:
        other = Member.objects.create(owner=self.owner, name="Ravi Iyer", email="ravi@acme.in")
        token = self.enroll(email=other.email, member_id=other.member_code, hostname="SALES-PC", machine_id="m9").json()[
            "device_token"
        ]
        early, late = self.shift()
        self.press(self.pc1, "working", early)
        reply = self.press(token, "working", late)
        self.assertNotIn("day_carry", reply)
        self.assertNotIn("day_moved", self.beat(self.pc1, "working", work_changed_at=early, work_events=[]))
        self.assertEqual(WorkLog.objects.exclude(moved_from="").count(), 0)
        self.assertEqual(Device.objects.filter(work_state="working").count(), 2)

    def test_two_members_taking_turns_on_one_pc_are_unaffected(self) -> None:
        early, late = self.shift()
        Device.objects.filter(pk=self.d2.pk).delete()
        self.press(self.pc1, "working", early)
        self.press(self.pc1, "ended", early + timedelta(seconds=30), reason="Done for today")
        other = Member.objects.create(owner=self.owner, name="Ravi Iyer", email="ravi@acme.in")
        token = self.enroll(email=other.email, member_id=other.member_code, hostname="OFFICE-PC", machine_id="m1").json()[
            "device_token"
        ]
        reply = self.press(token, "working", late)
        self.assertNotIn("day_carry", reply)
        self.assertEqual(WorkLog.objects.filter(member=other).count(), 1)
        self.assertEqual(WorkLog.objects.exclude(moved_from="").count(), 0)

    def test_a_pc_that_joins_as_someone_else_forgets_the_old_move(self) -> None:
        early, late = self.shift()
        self.press(self.pc1, "working", early)
        self.press(self.pc2, "working", late)
        other = Member.objects.create(owner=self.owner, name="Ravi Iyer", email="ravi@acme.in")
        token = self.enroll(email=other.email, member_id=other.member_code, hostname="OFFICE-PC", machine_id="m1").json()[
            "device_token"
        ]
        self.assertNotIn("day_moved", self.beat(token, "not_started"))

    def test_a_reinstall_for_the_same_member_keeps_the_move_so_start_still_takes_it_back(self) -> None:
        early, late = self.shift()
        self.press(self.pc1, "working", early)
        self.press(self.pc2, "working", early + (late - early) / 2)
        token = self.enroll(hostname="OFFICE-PC", machine_id="m1").json()["device_token"]
        self.assertEqual(self.beat(token, "not_started")["day_moved"]["to_hostname"], "LAPTOP-X")
        self.assertIn("day_carry", self.press(token, "working", late))
        self.d1.refresh_from_db()
        self.assertEqual(self.d1.work_state, "working")

    def test_a_plan_without_work_hours_has_no_hand_over(self) -> None:
        early, late = self.shift()
        self.press(self.pc1, "working", early)
        Plan.objects.update(modules=[])
        reply = self.press(self.pc2, "working", late)
        self.assertNotIn("day_carry", reply)
        self.assertEqual(WorkLog.objects.exclude(moved_from="").count(), 0)


class MidnightTests(HandoverTestCase):
    def test_a_pc_left_running_yesterday_is_not_an_active_day(self) -> None:
        yesterday = self.midnight - timedelta(hours=2)
        WorkLog.objects.create(owner=self.owner, device=self.d1, member=self.member, state="working", at=yesterday)
        Device.objects.filter(pk=self.d1.pk).update(work_state="working", work_since=yesterday, last_seen_at=yesterday)
        _, late = self.shift()
        reply = self.press(self.pc2, "working", late)
        self.assertNotIn("day_carry", reply)
        self.assertEqual(WorkLog.objects.exclude(moved_from="").count(), 0)

    def test_yesterdays_move_and_carry_are_forgotten(self) -> None:
        yesterday = self.midnight - timedelta(hours=2)
        Device.objects.filter(pk=self.d1.pk).update(work_moved_to=self.d2, work_moved_at=yesterday)
        Device.objects.filter(pk=self.d2.pk).update(work_carry={"at": yesterday.isoformat(), "worked_seconds": 3600})
        reply = self.beat(self.pc1, "not_started")
        self.assertNotIn("day_moved", reply)
        reply = self.beat(self.pc2, "not_started")
        self.assertNotIn("day_carry", reply)
        self.d1.refresh_from_db()
        self.assertIsNone(self.d1.work_moved_to_id)

    def test_a_forgotten_log_out_yesterday_does_not_hide_todays_start_from_the_carry(self) -> None:
        WorkLog.objects.create(owner=self.owner, device=self.d1, member=self.member, state="working",
                               at=self.midnight - timedelta(hours=20))
        early, late = self.shift()
        self.press(self.pc1, "working", early)
        carry = self.press(self.pc2, "working", late)["day_carry"]
        self.assertEqual(carry["worked_seconds"], int((late - early).total_seconds()))
