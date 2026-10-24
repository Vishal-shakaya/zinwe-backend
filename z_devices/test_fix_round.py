"""v1.2 fixes in z_devices: FIX-005 (device list, retired enrolment keys) and FIX-006 (device-token throttles)."""
from __future__ import annotations

from unittest import mock

from django.conf import settings
from rest_framework import status

from .authentication import DeviceScopedThrottle
from .models import Device, EnrollmentKey
from .tests import (
    HEARTBEAT_URL,
    KEY_URL,
    LIST_URL,
    METRICS_UPLOAD_URL,
    ROTATE_URL,
    UPLOAD_URL,
    DeviceTestCase,
)


class DeviceListTests(DeviceTestCase):
    """FIX-005: GET /api/devices/ (DEV-API-001) lists the PCs in use; removed PCs only on request."""

    def setUp(self) -> None:
        super().setUp()
        self.enroll()
        self.enroll(machine_id="OLD-MACHINE", hostname="OLD-PC")
        Device.objects.filter(hostname="OLD-PC").update(is_revoked=True)
        self.as_owner()

    def test_revoked_pcs_are_left_out(self) -> None:
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([d["hostname"] for d in response.json()], ["ACCOUNTS-PC-01"])

    def test_include_revoked_for_history_views(self) -> None:
        rows = self.client.get(LIST_URL, {"include_revoked": "1"}).json()
        self.assertEqual(
            sorted((d["hostname"], d["is_revoked"]) for d in rows),
            [("ACCOUNTS-PC-01", False), ("OLD-PC", True)],
        )
        self.assertEqual(len(self.client.get(LIST_URL, {"include_revoked": "0"}).json()), 1)


class RetiredEnrollmentKeyTests(DeviceTestCase):
    """FIX-005: DEV-API-003/004 are retired -- enrolment uses the member id + email, never an org key."""

    def test_get_key_is_gone_and_creates_nothing(self) -> None:
        EnrollmentKey.objects.all().delete()
        self.as_owner()
        response = self.client.get(KEY_URL)
        self.assertEqual(response.status_code, status.HTTP_410_GONE)
        self.assertIn("member", response.json()["detail"].lower())
        self.assertFalse(EnrollmentKey.objects.exists())

    def test_rotate_is_gone_and_changes_nothing(self) -> None:
        key = EnrollmentKey.objects.create(owner=self.owner)
        self.as_owner()
        response = self.client.post(ROTATE_URL)
        self.assertEqual(response.status_code, status.HTTP_410_GONE)
        key.refresh_from_db()
        self.assertTrue(key.is_active)
        self.assertEqual(EnrollmentKey.objects.count(), 1)

    def test_still_needs_login(self) -> None:
        self.assertEqual(self.client.get(KEY_URL).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.client.post(ROTATE_URL).status_code, status.HTTP_401_UNAUTHORIZED)


class DeviceThrottleTests(DeviceTestCase):
    """FIX-006: heartbeat / events upload / metrics upload are throttled per PC, generously."""

    def setUp(self) -> None:
        super().setUp()
        self.token_a = self.enroll().json()["device_token"]
        self.token_b = self.enroll(machine_id="SECOND-MACHINE", hostname="SECOND-PC").json()["device_token"]

    def test_default_rates_leave_room_for_a_normal_agent(self) -> None:
        rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
        self.assertEqual(rates["device_heartbeat"], "120/min")  # agent beats every 60 s (+ on work changes)
        self.assertEqual(rates["device_events"], "300/min")  # up to 20 batches / 30 s, one-by-one retries
        self.assertEqual(rates["device_metrics"], "60/min")  # a few batches after each heartbeat

    def hammer(self, url: str, scope: str, body: dict) -> None:  # type: ignore[type-arg]
        with mock.patch.dict(DeviceScopedThrottle.THROTTLE_RATES, {scope: "3/min"}):
            self.as_device(self.token_a)
            for _ in range(3):
                self.assertNotEqual(self.client.post(url, body, format="json").status_code, 429)
            blocked = self.client.post(url, body, format="json")
            self.assertEqual(blocked.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
            self.assertIn("Retry-After", blocked)
            # Keyed per PC, not per IP: another PC in the same office is unaffected.
            self.as_device(self.token_b)
            self.assertNotEqual(self.client.post(url, body, format="json").status_code, 429)

    def test_heartbeat_throttled_per_device(self) -> None:
        self.hammer(HEARTBEAT_URL, "device_heartbeat", {})

    def test_events_upload_throttled_per_device(self) -> None:
        self.hammer(UPLOAD_URL, "device_events", {"events": []})

    def test_metrics_upload_throttled_per_device(self) -> None:
        self.hammer(METRICS_UPLOAD_URL, "device_metrics", {"metrics": []})

    def test_unknown_token_is_401_not_throttled(self) -> None:
        self.as_device("not-a-token")
        self.assertEqual(self.client.post(HEARTBEAT_URL, {}, format="json").status_code, 401)
