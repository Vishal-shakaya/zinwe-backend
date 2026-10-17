"""History kept per plan (2026-10-17): older alerts, usage and work log are hidden, then pruned 30 days later."""
from __future__ import annotations

import uuid
from datetime import timedelta

from django.utils import timezone

from z_auth._testing import give_plan
from z_auth.models import Subscription

from .models import Device, DeviceEvent, UsageMetric, WorkLog
from .retention import GRACE_DAYS, prune_history
from .tests import EVENTS_URL, METRICS_URL, DeviceTestCase

WORK_LOG_URL = "/api/devices/work-log/"


class HistoryTests(DeviceTestCase):
    """DeviceTestCase starts the owner on Businesses (365 days); `on_free()` drops to Free forever (7 days)."""

    def setUp(self) -> None:
        super().setUp()
        self.enroll()
        self.device = Device.objects.get()
        self.now = timezone.now()

    def on_free(self) -> None:
        Subscription.objects.filter(owner=self.owner).delete()

    def event(self, days_ago: float) -> DeviceEvent:
        return DeviceEvent.objects.create(
            device=self.device, client_id=uuid.uuid4(), type="usb.connected",
            occurred_at=self.now - timedelta(days=days_ago), data={},
        )

    def metric(self, days_ago: float) -> UsageMetric:
        bucket = (self.now - timedelta(days=days_ago)).replace(minute=0, second=0, microsecond=0)
        return UsageMetric.objects.create(
            device=self.device, bucket_start=bucket, category="web.domain", key="zinwe.io", count=1, seconds=60,
        )

    def work(self, days_ago: float) -> WorkLog:
        return WorkLog.objects.create(
            owner=self.owner, device=self.device, member=self.member, state=WorkLog.State.WORKING,
            at=self.now - timedelta(days=days_ago),
        )

    def event_ids(self) -> list[int]:
        self.as_owner()
        return sorted(row["id"] for row in self.client.get(EVENTS_URL).json())

    def test_free_sees_seven_days_of_alerts(self) -> None:
        recent, old = self.event(2), self.event(10)
        self.assertEqual(self.event_ids(), sorted([recent.pk, old.pk]))  # Businesses: a year
        self.on_free()
        self.assertEqual(self.event_ids(), [recent.pk])

    def test_usage_charts_stop_at_the_window(self) -> None:
        self.metric(2)
        self.metric(20)
        self.on_free()
        self.as_owner()
        series = self.client.get(METRICS_URL, {"days": 30}).json()["series"]
        self.assertEqual(len(series), 1)  # the 20-day-old bucket is outside Free's 7 days

    def test_work_log_before_the_window_is_empty(self) -> None:
        self.work(10)
        day = timezone.localtime(self.now - timedelta(days=10)).date().isoformat()
        self.as_owner()
        self.assertEqual(len(self.client.get(WORK_LOG_URL, {"date": day}).json()["entries"]), 1)
        self.on_free()
        self.assertEqual(self.client.get(WORK_LOG_URL, {"date": day}).json()["entries"], [])

    def test_me_says_how_much_history_the_plan_keeps(self) -> None:
        self.as_owner()
        self.assertEqual(self.client.get("/api/auth/me/").json()["entitlements"]["history_days"], 365)
        give_plan(self.owner, "startups")
        self.assertEqual(self.client.get("/api/auth/me/").json()["entitlements"]["history_days"], 90)

    def test_prune_deletes_only_past_window_plus_grace(self) -> None:
        self.on_free()
        hidden_but_kept = self.event(7 + GRACE_DAYS - 2)
        gone = self.event(7 + GRACE_DAYS + 2)
        self.metric(7 + GRACE_DAYS + 2)
        self.work(7 + GRACE_DAYS + 2)
        result = prune_history(self.now)
        self.assertEqual((result.events, result.metrics, result.work_log, result.owners), (1, 1, 1, 1))
        self.assertTrue(DeviceEvent.objects.filter(pk=hidden_but_kept.pk).exists())
        self.assertFalse(DeviceEvent.objects.filter(pk=gone.pk).exists())
        self.assertIn("1 event(s)", result.summary())

    def test_prune_keeps_a_year_on_businesses_and_dry_run_deletes_nothing(self) -> None:
        kept = self.event(100)
        self.assertEqual(prune_history(self.now).events, 0)
        self.assertTrue(DeviceEvent.objects.filter(pk=kept.pk).exists())
        self.on_free()
        self.assertEqual(prune_history(self.now, dry_run=True).events, 1)
        self.assertTrue(DeviceEvent.objects.filter(pk=kept.pk).exists())
