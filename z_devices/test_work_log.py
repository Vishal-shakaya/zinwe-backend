"""The owner's Activity timeline: work-day changes logged from heartbeats (GET work-log/)."""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from rest_framework import status

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

    def test_day_is_logged_from_state_changes(self) -> None:
        started = timezone.now() - timedelta(hours=3)
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
        token, _ = Token.objects.get_or_create(user=other)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        self.assertEqual(self.client.get(WORK_LOG_URL, {"date": day}).json()["entries"], [])
