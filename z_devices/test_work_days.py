"""
FIX-011: a shift that crosses midnight is split across both days (day totals, Activity, reports).
FIX-012: report PDFs print non-Latin names; the decision note's limit; a log-out-only day is not a day worked.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from django.utils import timezone
from rest_framework import status

from .models import Device, WorkLog
from .reports import build_report
from .tests import DeviceTestCase

REPORTS = "/api/devices/reports/"
WORK_LOG = "/api/devices/work-log/"
H = 3600


class WorkDayTestBase(DeviceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.enroll()
        self.device = Device.objects.get()
        self.as_owner()
        self.day2 = timezone.localdate() - timedelta(days=1)  # yesterday
        self.day1 = self.day2 - timedelta(days=1)

    def at(self, day: date, hour: int, minute: int = 0) -> datetime:
        return timezone.make_aware(datetime.combine(day, datetime.min.time()) + timedelta(hours=hour, minutes=minute))

    def log(self, state: str, when: datetime) -> WorkLog:
        return WorkLog.objects.create(owner=self.owner, device=self.device, member=self.member, state=state, at=when,
                                      approval="pending" if state != "working" else "")

    def report(self, start: date, end: date) -> dict:
        response = self.client.get(REPORTS, {"from": start.isoformat(), "to": end.isoformat()})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.json()

    def night_shift(self) -> None:
        """Log in 22:00 on day 1, log out 02:00 on day 2."""
        self.log("working", self.at(self.day1, 22))
        self.log("ended", self.at(self.day2, 2))


class OvernightReportTests(WorkDayTestBase):
    def test_a_shift_across_midnight_counts_on_both_days(self) -> None:
        self.night_shift()
        body = self.report(self.day1, self.day2)
        self.assertEqual([d["worked_seconds"] for d in body["daily"]], [2 * H, 2 * H])
        person = body["people"][0]
        self.assertEqual(person["worked_seconds"], 4 * H)
        self.assertEqual(person["days_worked"], 1)  # one shift, counted on the day it began
        self.assertEqual((person["first_in"], person["last_out"]), (22 * 60, 2 * 60))

    def test_each_day_alone_gets_its_own_part(self) -> None:
        self.night_shift()
        self.assertEqual(self.report(self.day1, self.day1)["people"][0]["worked_seconds"], 2 * H)
        self.assertEqual(self.report(self.day2, self.day2)["people"][0]["worked_seconds"], 2 * H)

    def test_a_break_across_midnight_is_split_too(self) -> None:
        self.log("working", self.at(self.day1, 21))
        self.log("on_break", self.at(self.day1, 23, 30))
        self.log("working", self.at(self.day2, 0, 30))
        self.log("ended", self.at(self.day2, 3))
        daily = self.report(self.day1, self.day2)["daily"]
        self.assertEqual([(d["worked_seconds"], d["break_seconds"], d["breaks"]) for d in daily],
                         [(int(2.5 * H), 1800, 1), (int(2.5 * H), 1800, 0)])

    def test_a_shift_still_running_across_midnight(self) -> None:
        today = timezone.localdate()
        now = self.at(today, 1)
        self.log("working", self.at(today - timedelta(days=1), 22))
        Device.objects.filter(pk=self.device.pk).update(work_state="working", last_seen_at=now - timedelta(seconds=30))
        report = build_report(self.owner, today - timedelta(days=1), today, now=now)
        self.assertEqual([d["worked_seconds"] for d in report["daily"]], [2 * H, 1 * H])

    def test_a_forgotten_log_out_is_still_not_counted(self) -> None:
        # Logged in Monday morning, never logged out, PC switched off: no 40-hour days.
        self.log("working", self.at(self.day1, 9))
        Device.objects.filter(pk=self.device.pk).update(work_state="working",
                                                        last_seen_at=self.at(self.day1, 18))
        body = self.report(self.day1, self.day2)
        self.assertEqual([d["worked_seconds"] for d in body["daily"]], [0, 0])

    def test_a_stretch_closed_days_later_is_a_forgotten_log_out(self) -> None:
        self.log("working", self.at(self.day1, 9))
        self.log("ended", self.at(self.day2, 18))  # 33 hours later
        body = self.report(self.day1, self.day2)
        self.assertEqual([d["worked_seconds"] for d in body["daily"]], [0, 0])


class OvernightActivityTests(WorkDayTestBase):
    def activity(self, day: date) -> dict:
        response = self.client.get(WORK_LOG, {"date": day.isoformat()})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.json()

    def test_the_second_day_shows_the_stretch_carried_over_midnight(self) -> None:
        self.night_shift()
        body = self.activity(self.day2)
        self.assertEqual([e["state"] for e in body["entries"]], ["ended"])  # unchanged: that day's own changes
        self.assertEqual([e["state"] for e in body["carried_in"]], ["working"])
        person = body["totals"][0]
        self.assertEqual((person["member_id"], person["worked_seconds"], person["open_ended"]),
                         (self.member.pk, 2 * H, False))
        self.assertTrue(person["carried_in"])

    def test_the_first_day_counts_up_to_midnight(self) -> None:
        self.night_shift()
        person = self.activity(self.day1)["totals"][0]
        self.assertEqual((person["worked_seconds"], person["open_ended"], person["continues"]), (2 * H, False, True))

    def test_a_past_day_never_logged_out_is_flagged(self) -> None:
        self.log("working", self.at(self.day2, 9))
        person = self.activity(self.day2)["totals"][0]
        self.assertEqual((person["worked_seconds"], person["open_ended"]), (0, True))


class LogOutOnlyDayTests(WorkDayTestBase):  # FIX-012 (c)
    def test_a_day_with_only_a_log_out_is_not_a_day_worked(self) -> None:
        self.log("ended", self.at(self.day2, 18))
        body = self.report(self.day2, self.day2)
        person = body["people"][0]
        self.assertEqual((person["days_worked"], person["logouts"]), (0, 1))
        self.assertEqual((body["totals"]["person_days"], body["totals"]["people_worked"]), (0, 0))


class DecisionNoteLimitTests(WorkDayTestBase):  # FIX-012 (b)
    def decide(self, note: str):  # type: ignore[no-untyped-def]
        log = self.log("on_break", self.at(self.day2, 13))
        return self.client.post(f"{WORK_LOG}{log.pk}/decision/", {"decision": "denied", "note": note}, format="json")

    def test_280_characters_is_fine(self) -> None:
        response = self.decide("x" * 280)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()["decision_note"]), 280)

    def test_281_is_refused_with_a_clear_message(self) -> None:
        response = self.decide("x" * 281)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["note"], ["Keep the note to 280 characters or fewer (this one has 281)."])


class UnicodePdfTests(WorkDayTestBase):  # FIX-012 (a)
    def test_hindi_names_are_printed_not_question_marks(self) -> None:
        from .report_pdf import _text, report_pdf

        self.member.name = "विशाल शाक्य"
        self.member.save(update_fields=["name"])
        self.night_shift()
        report = build_report(self.owner, self.day1, self.day2, member=self.member)
        self.assertEqual(_text(self.member.name), "विशाल शाक्य")
        pdf = report_pdf(report, "शाक्य टेक")
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertIn(b"NotoSansDevanagari", pdf)  # the Devanagari font is embedded (subset)
        self.assertLess(len(pdf), 250_000)
