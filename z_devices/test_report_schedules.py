"""FR-22: one person's report, the new numbers, PDF, emailing a report and scheduled reports (reports_pro)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from django.core import mail
from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token

from z_auth.models import User

from .models import Device, Member, ReportDelivery, ReportSchedule, UsageMetric, WorkLog
from .report_schedules import next_run, period_for, send_due
from .tests import DeviceTestCase

URL = "/api/devices/reports/"


class ReportTestBase(DeviceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.enroll()
        self.device = Device.objects.get()
        self.as_owner()
        self.yesterday = timezone.localdate() - timedelta(days=1)

    def at(self, day: date, hour: int, minute: int = 0) -> datetime:
        return timezone.make_aware(datetime.combine(day, datetime.min.time()) + timedelta(hours=hour, minutes=minute))

    def a_day(self, day: date, member: Member | None = None, device: Device | None = None, start: int = 9) -> None:
        """In at `start`, a 30-minute break at 13:00, out at 18:00."""
        for state, when in (("working", self.at(day, start)), ("on_break", self.at(day, 13)),
                            ("working", self.at(day, 13, 30)), ("ended", self.at(day, 18))):
            WorkLog.objects.create(owner=self.owner, device=device or self.device, member=member or self.member,
                                   state=state, at=when)

    def second_person(self) -> tuple[Member, Device]:
        ravi = Member.objects.create(owner=self.owner, name="Ravi Kumar", email="ravi@acme.in")
        pc = Device.objects.create(owner=self.owner, member=ravi, hostname="SALES-PC", machine_id="m-2",
                                   os_version="Windows 11", agent_version="1.1.2")
        return ravi, pc

    def as_free_owner(self) -> User:
        free = User.objects.filter(email="free@acme.in").first() or User.objects.create_user("free@acme.in", "pw-12345678")
        token, _ = Token.objects.get_or_create(user=free)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        return free


class PersonReportTests(ReportTestBase):
    def test_one_persons_report_lists_their_days_and_usual_times(self) -> None:
        ravi, pc = self.second_person()
        self.a_day(self.yesterday, start=9)
        self.a_day(self.yesterday - timedelta(days=1), start=10)
        self.a_day(self.yesterday, member=ravi, device=pc)
        UsageMetric.objects.create(device=self.device, bucket_start=self.at(self.yesterday, 10),
                                   category="app.foreground", key="excel", count=4, seconds=5400)
        UsageMetric.objects.create(device=self.device, bucket_start=self.at(self.yesterday, 10),
                                   category="web.data.app", key="chrome", bytes=50 * 1_048_576)
        UsageMetric.objects.create(device=self.device, bucket_start=self.at(self.yesterday, 10),
                                   category="web.data.site", key="youtube.com", bytes=40 * 1_048_576)

        body = self.client.get(URL, {"from": (self.yesterday - timedelta(days=6)).isoformat(),
                                     "to": self.yesterday.isoformat(), "member": self.member.pk}).json()
        self.assertEqual(body["member"]["name"], "Asha Rao")
        self.assertEqual([p["name"] for p in body["people"]], ["Asha Rao"])  # Ravi isn't in Asha's report
        person = body["people"][0]
        self.assertEqual(person["first_in"], 9 * 60 + 30)  # 9:00 and 10:00 -> 9:30
        self.assertEqual(person["last_out"], 18 * 60)
        self.assertEqual((person["active_seconds"], person["data_bytes"]), (5400, 50 * 1_048_576))
        self.assertEqual([d["date"] for d in body["person_days"]],
                         [(self.yesterday - timedelta(days=1)).isoformat(), self.yesterday.isoformat()])
        self.assertEqual(body["person_days"][1]["first_in"], 9 * 60)
        self.assertEqual(body["top_data"], [{"key": "youtube.com", "seconds": 0, "count": 0, "bytes": 40 * 1_048_576}])
        self.assertEqual(len(body["daily"]), 7)
        self.assertEqual(body["daily"][-1]["worked_seconds"], 30600)

    def test_the_team_report_has_everyone_and_the_daily_trend(self) -> None:
        ravi, pc = self.second_person()
        self.a_day(self.yesterday)
        self.a_day(self.yesterday, member=ravi, device=pc)
        body = self.client.get(URL, {"from": self.yesterday.isoformat(), "to": self.yesterday.isoformat()}).json()
        self.assertIsNone(body["member"])
        self.assertEqual({p["name"] for p in body["people"]}, {"Asha Rao", "Ravi Kumar"})
        self.assertEqual((body["totals"]["people_worked"], body["totals"]["person_days"]), (2, 2))
        self.assertEqual(body["daily"][0]["people"], 2)
        self.assertNotIn("person_days", body)

    def test_each_day_of_the_team_report_breaks_down_by_person(self) -> None:
        ravi, pc = self.second_person()
        two_days_ago = self.yesterday - timedelta(days=1)
        self.a_day(self.yesterday, start=9)
        self.a_day(self.yesterday, member=ravi, device=pc, start=11)
        self.a_day(two_days_ago)
        UsageMetric.objects.create(device=pc, bucket_start=self.at(self.yesterday, 12),
                                   category="app.foreground", key="excel", count=2, seconds=1800)
        body = self.client.get(URL, {"from": two_days_ago.isoformat(), "to": self.yesterday.isoformat()}).json()
        first, last = body["daily"]
        self.assertEqual([p["name"] for p in first["by_person"]], ["Asha Rao"])
        asha, ravi_day = last["by_person"]  # most worked first
        self.assertEqual((asha["name"], asha["worked_seconds"], asha["first_in"], asha["last_out"]),
                         ("Asha Rao", 30600, 9 * 60, 18 * 60))
        self.assertEqual((ravi_day["key"], ravi_day["member_id"], ravi_day["worked_seconds"], ravi_day["active_seconds"]),
                         (f"m{ravi.pk}", ravi.pk, 23400, 1800))
        self.assertEqual(sum(p["worked_seconds"] for p in last["by_person"]), last["worked_seconds"])
        person = self.client.get(URL, {"from": self.yesterday.isoformat(), "to": self.yesterday.isoformat(),
                                       "member": ravi.pk}).json()
        self.assertNotIn("by_person", person["daily"][0])

    def test_someone_elses_member_is_not_found(self) -> None:
        other = User.objects.create_user("other@x.in", "pw-12345678")
        stranger = Member.objects.create(owner=other, name="Stranger", email="s@x.in")
        response = self.client.get(URL, {"from": self.yesterday.isoformat(), "to": self.yesterday.isoformat(),
                                         "member": stranger.pk})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_pdf_download_on_a_paid_plan_and_locked_on_free(self) -> None:
        self.a_day(self.yesterday)
        params = {"from": self.yesterday.isoformat(), "to": self.yesterday.isoformat(), "file": "pdf"}
        response = self.client.get(f"{URL}download/", params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))
        self.assertIn("Zinwe-report-team-", response["Content-Disposition"])

        self.as_free_owner()
        locked = self.client.get(f"{URL}download/", params)
        self.assertEqual(locked.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(locked.json()["code"], "plan_feature")
        # CSV stays on every plan
        self.assertEqual(self.client.get(f"{URL}download/", {**params, "file": "csv"}).status_code, status.HTTP_200_OK)

    def test_a_persons_pdf_has_their_days(self) -> None:
        self.a_day(self.yesterday)
        response = self.client.get(f"{URL}download/", {"from": (self.yesterday - timedelta(days=3)).isoformat(),
                                                       "to": self.yesterday.isoformat(), "file": "pdf",
                                                       "member": self.member.pk})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("Zinwe-report-asha-rao-", response["Content-Disposition"])


class SendReportTests(ReportTestBase):
    def send(self, **data):  # type: ignore[no-untyped-def]
        payload = {"from": self.yesterday.isoformat(), "to": self.yesterday.isoformat(), **data}
        return self.client.post(f"{URL}send/", payload, format="json")

    def test_the_team_report_to_the_owner_and_a_manager_and_each_person_their_own(self) -> None:
        ravi, pc = self.second_person()
        self.a_day(self.yesterday)
        self.a_day(self.yesterday, member=ravi, device=pc)
        response = self.send(send_to_owner=True, send_to_members=True, emails=["Manager@Acme.in"], include_csv=True)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        body = response.json()
        self.assertEqual(body["status"], "sent")
        self.assertEqual(set(body["recipients"]), {"owner@acme.in", "manager@acme.in", "asha@acme.in", "ravi@acme.in"})

        by_to = {m.to[0]: m for m in mail.outbox}
        team = by_to["manager@acme.in"]
        self.assertIn("Team report", team.subject)
        self.assertEqual([a[0].rsplit(".", 1)[1] for a in team.attachments], ["pdf", "csv"])
        self.assertEqual(team.reply_to, ["owner@acme.in"])
        asha = by_to["asha@acme.in"]
        self.assertIn("Your work summary", asha.subject)
        self.assertIn("asha-rao", asha.attachments[0][0])  # her own report, not the team's
        self.assertNotIn("Ravi", asha.body)
        self.assertEqual(len(asha.attachments), 1)
        self.assertEqual(ReportDelivery.objects.get().recipients, body["recipients"])

    def test_people_with_nothing_recorded_are_skipped(self) -> None:
        self.second_person()
        self.a_day(self.yesterday)
        body = self.send(send_to_owner=False, send_to_members=True).json()
        self.assertEqual(body["recipients"], ["asha@acme.in"])  # Ravi had nothing, Asha had a day

    def test_someone_must_get_it(self) -> None:
        response = self.send(send_to_owner=False, send_to_members=False, emails=[])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_at_most_five_other_addresses(self) -> None:
        response = self.send(emails=[f"p{i}@acme.in" for i in range(6)])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_free_plan_is_locked(self) -> None:
        self.as_free_owner()
        response = self.send(send_to_owner=True)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.json()["module"], "reports_pro")


class ScheduleTests(ReportTestBase):
    def create(self, **data):  # type: ignore[no-untyped-def]
        return self.client.post(f"{URL}schedules/", {"frequency": "weekly", "weekday": 0, "send_hour": 9, **data},
                                format="json")

    def test_create_lists_and_describes(self) -> None:
        response = self.create(send_to_members=True, extra_emails=["a@acme.in"])
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        body = response.json()
        self.assertEqual(body["description"], "Every Monday around 9 AM, for the week before")
        self.assertIsNotNone(body["next_run_at"])
        nxt = timezone.localtime(datetime.fromisoformat(body["next_run_at"]))
        self.assertEqual((nxt.weekday(), nxt.hour), (0, 9))
        self.assertEqual(len(self.client.get(f"{URL}schedules/").json()), 1)

    def test_validation(self) -> None:
        self.assertEqual(self.create(send_hour=24).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.create(frequency="monthly", month_day=31).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.create(send_to_owner=False).status_code, status.HTTP_400_BAD_REQUEST)
        other = User.objects.create_user("other@x.in", "pw-12345678")
        stranger = Member.objects.create(owner=other, name="Stranger", email="s@x.in")
        self.assertEqual(self.create(member=stranger.pk).status_code, status.HTTP_400_BAD_REQUEST)

    def test_periods(self) -> None:
        s = ReportSchedule(owner=self.owner, frequency="weekly")
        thursday = date(2026, 10, 22)
        self.assertEqual(period_for(s, thursday), (date(2026, 10, 12), date(2026, 10, 18)))
        self.assertEqual(period_for(s, date(2026, 10, 19)), (date(2026, 10, 12), date(2026, 10, 18)))  # a Monday
        s.frequency = "monthly"
        self.assertEqual(period_for(s, date(2026, 11, 1)), (date(2026, 10, 1), date(2026, 10, 31)))
        s.frequency = "daily"
        self.assertEqual(period_for(s, thursday), (date(2026, 10, 21), date(2026, 10, 21)))
        s.frequency, s.every_days = "custom", 14
        self.assertEqual(period_for(s, thursday), (date(2026, 10, 8), date(2026, 10, 21)))

    def test_next_run(self) -> None:
        s = ReportSchedule(owner=self.owner, frequency="monthly", month_day=1, send_hour=8)
        after = timezone.make_aware(datetime(2026, 10, 22, 12))
        self.assertEqual(timezone.localtime(next_run(s, after)).replace(tzinfo=None), datetime(2026, 11, 1, 8))
        s.frequency, s.every_days = "custom", 3
        s.next_run_at = timezone.make_aware(datetime(2026, 10, 20, 8))
        self.assertEqual(timezone.localtime(next_run(s, after)).replace(tzinfo=None), datetime(2026, 10, 23, 8))

    def test_the_jobs_loop_sends_once_and_moves_on(self) -> None:
        self.a_day(self.yesterday)
        schedule = ReportSchedule.objects.create(owner=self.owner, frequency="daily", send_hour=9,
                                                 next_run_at=timezone.now() - timedelta(minutes=5))
        result = send_due()
        self.assertEqual((result.sent, result.failed), (1, 0))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("scheduled", mail.outbox[0].body)
        schedule.refresh_from_db()
        self.assertGreater(schedule.next_run_at, timezone.now())
        self.assertIsNotNone(schedule.last_sent_at)
        self.assertEqual(ReportDelivery.objects.get().schedule, schedule)
        self.assertEqual(send_due().sent, 0)  # not due again until tomorrow

    def test_a_downgrade_pauses_it_and_pausing_is_always_allowed(self) -> None:
        free = User.objects.create_user("free@acme.in", "pw-12345678")
        schedule = ReportSchedule.objects.create(owner=free, frequency="daily", next_run_at=timezone.now() - timedelta(minutes=1))
        result = send_due()
        self.assertEqual((result.sent, result.skipped), (0, 1))
        schedule.refresh_from_db()
        self.assertIn("Paused", schedule.last_status)
        self.assertEqual(len(mail.outbox), 0)

        self.as_free_owner()
        own = ReportSchedule.objects.create(owner=User.objects.get(email="free@acme.in"), frequency="daily")
        self.assertEqual(self.client.patch(f"{URL}schedules/{own.pk}/", {"is_active": False}, format="json").status_code,
                         status.HTTP_200_OK)
        self.assertEqual(self.client.patch(f"{URL}schedules/{own.pk}/", {"send_hour": 7}, format="json").status_code,
                         status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.post(f"{URL}schedules/", {"frequency": "daily"}, format="json").status_code,
                         status.HTTP_403_FORBIDDEN)

    def test_send_now_and_sent_list(self) -> None:
        self.a_day(self.yesterday)
        schedule = self.create(frequency="daily").json()
        response = self.client.post(f"{URL}schedules/{schedule['id']}/send-now/", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        sent = self.client.get(f"{URL}deliveries/").json()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["recipients"], ["owner@acme.in"])

    def test_delete(self) -> None:
        schedule = self.create().json()
        self.assertEqual(self.client.delete(f"{URL}schedules/{schedule['id']}/").status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(ReportSchedule.objects.exists())
