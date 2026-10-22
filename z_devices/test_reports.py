"""FR-17: reports for any period, on screen and as CSV."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from django.utils import timezone
from rest_framework import status

from .models import Device, DeviceEvent, UsageMetric, WorkLog
from .tests import DeviceTestCase

URL = "/api/devices/reports/"


class ReportTests(DeviceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.enroll()
        self.device = Device.objects.get()
        self.as_owner()
        self.yesterday = timezone.localdate() - timedelta(days=1)

    def at(self, day, hour: int, minute: int = 0) -> datetime:  # type: ignore[no-untyped-def]
        return timezone.make_aware(datetime.combine(day, datetime.min.time()) + timedelta(hours=hour, minutes=minute))

    def log(self, state: str, when: datetime, approval: str = "") -> None:
        WorkLog.objects.create(owner=self.owner, device=self.device, member=self.member, state=state, at=when,
                               approval=approval)

    def a_day(self, day) -> None:  # type: ignore[no-untyped-def]
        """9:00 in, 13:00-13:30 lunch, 18:00 out: 8 h 30 min worked, 30 min on a break."""
        self.log("working", self.at(day, 9))
        self.log("on_break", self.at(day, 13), approval="approved")
        self.log("working", self.at(day, 13, 30))
        self.log("ended", self.at(day, 18), approval="pending")

    def get(self, start, end):  # type: ignore[no-untyped-def]
        return self.client.get(URL, {"from": start.isoformat(), "to": end.isoformat()})

    def test_a_week_per_person(self) -> None:
        self.a_day(self.yesterday)
        self.a_day(self.yesterday - timedelta(days=1))
        DeviceEvent.objects.create(device=self.device, client_id=uuid.uuid4(), type="usb.connected",
                                   severity="warning", occurred_at=self.at(self.yesterday, 10), data={})
        UsageMetric.objects.create(device=self.device, bucket_start=self.at(self.yesterday, 10),
                                   category="app.foreground", key="excel", count=1, seconds=3600)
        body = self.get(self.yesterday - timedelta(days=6), self.yesterday).json()
        person = body["people"][0]
        self.assertEqual(person["name"], "Asha Rao")
        self.assertEqual((person["days_worked"], person["worked_seconds"], person["break_seconds"]), (2, 2 * 30600, 2 * 1800))
        self.assertEqual((person["breaks"], person["logouts"], person["to_review"], person["alerts"]), (2, 2, 2, 1))
        self.assertEqual(body["days"], 7)
        self.assertEqual(body["top_apps"], [{"key": "excel", "seconds": 3600, "count": 1}])
        self.assertEqual(body["no_activity"], [])

    def test_an_unfinished_past_day_is_not_counted_past_its_last_change(self) -> None:
        self.log("working", self.at(self.yesterday, 9))
        self.log("on_break", self.at(self.yesterday, 12))  # never came back, never logged out
        person = self.get(self.yesterday, self.yesterday).json()["people"][0]
        self.assertEqual((person["worked_seconds"], person["break_seconds"]), (3 * 3600, 0))

    def test_csv_download(self) -> None:
        self.a_day(self.yesterday)
        response = self.client.get(f"{URL}download/", {"from": self.yesterday.isoformat(), "to": self.yesterday.isoformat()})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn(f"Zinwe-report-{self.yesterday}-to-{self.yesterday}.csv", response["Content-Disposition"])
        text = response.content.decode()
        self.assertIn("Asha Rao,1,8:30,0:30,1,1,1,0", text)

    def test_bad_periods(self) -> None:
        today = timezone.localdate()
        self.assertEqual(self.client.get(URL).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.get(today, today - timedelta(days=1)).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.get(today + timedelta(days=1), today + timedelta(days=2)).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.get(today - timedelta(days=400), today).status_code, status.HTTP_400_BAD_REQUEST)
        body = self.get(today - timedelta(days=3), today + timedelta(days=5)).json()
        self.assertEqual(body["to"], today.isoformat())  # never into the future

    def test_members_with_nothing_are_listed(self) -> None:
        body = self.get(self.yesterday, self.yesterday).json()
        self.assertEqual((body["people"], body["no_activity"]), ([], ["Asha Rao"]))

    def test_owner_only(self) -> None:
        self.client.credentials()
        self.assertEqual(self.get(self.yesterday, self.yesterday).status_code, status.HTTP_401_UNAUTHORIZED)
