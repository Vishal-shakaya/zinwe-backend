from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from typing import Any
from unittest import mock

from django.core.cache import cache
from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from z_analytics.models import Event
from z_auth.models import User

from .events import build_title
from .models import Device, DeviceEvent, EnrollmentKey, Policy, hash_device_token

ENROLL_URL = "/api/devices/enroll/"
HEARTBEAT_URL = "/api/devices/heartbeat/"
KEY_URL = "/api/devices/enrollment-key/"
ROTATE_URL = "/api/devices/enrollment-key/rotate/"
LIST_URL = "/api/devices/"
POLICY_URL = "/api/devices/policy/"
CURRENT_POLICY_URL = "/api/devices/policy/current/"
EVENTS_URL = "/api/devices/events/"
UPLOAD_URL = "/api/devices/events/upload/"


class DeviceTestCase(APITestCase):
    def setUp(self) -> None:
        cache.clear()  # reset throttle counters between tests
        self.owner = User.objects.create_user("owner@acme.in", "pw-12345678")
        self.key = EnrollmentKey.objects.create(owner=self.owner)

    def enroll(self, **overrides: Any):
        payload = {
            "enrollment_key": self.key.key,
            "machine_id": "6F1C2A40-0000-4000-8000-ABCDEF012345",
            "hostname": "ACCOUNTS-PC-01",
            "os_version": "Windows 11 Pro 26100",
            "agent_version": "0.1.0",
            **overrides,
        }
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(ENROLL_URL, payload, format="json")

    def as_device(self, token: str) -> None:
        self.client.credentials(HTTP_AUTHORIZATION=f"Device {token}")

    def as_owner(self) -> None:
        token, _ = Token.objects.get_or_create(user=self.owner)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")


class EnrollTests(DeviceTestCase):
    def test_enroll_creates_device_and_returns_token_once(self) -> None:
        response = self.enroll()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()
        device = Device.objects.get()
        self.assertEqual(body["device_id"], device.id)
        self.assertFalse(body["re_enrolled"])
        self.assertEqual(device.owner, self.owner)
        self.assertEqual(device.machine_id, "6f1c2a40-0000-4000-8000-abcdef012345")
        # Only the hash is stored.
        self.assertEqual(device.token_hash, hash_device_token(body["device_token"]))
        self.assertNotIn(body["device_token"], device.token_hash)
        self.assertEqual(Event.objects.get().name, "device.enrolled")

    def test_key_is_case_and_whitespace_insensitive(self) -> None:
        response = self.enroll(enrollment_key=f"  {self.key.key.lower()} ")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_reinstall_reuses_row_and_rotates_token(self) -> None:
        first = self.enroll().json()
        Device.objects.update(is_revoked=True)

        response = self.enroll(hostname="ACCOUNTS-PC-RENAMED")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        second = response.json()
        self.assertTrue(second["re_enrolled"])
        self.assertEqual(second["device_id"], first["device_id"])
        self.assertNotEqual(second["device_token"], first["device_token"])
        device = Device.objects.get()
        self.assertFalse(device.is_revoked)
        self.assertEqual(device.hostname, "ACCOUNTS-PC-RENAMED")

    def test_unknown_or_revoked_key_is_rejected(self) -> None:
        self.assertEqual(
            self.enroll(enrollment_key="ZNW-AAAA-BBBB-CCCC").status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        EnrollmentKey.objects.update(is_active=False)
        response = self.enroll()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("enrollment_key", response.json())
        self.assertFalse(Device.objects.exists())

    def test_disabled_owner_cannot_enroll(self) -> None:
        User.objects.update(is_active=False)
        self.assertEqual(self.enroll().status_code, status.HTTP_400_BAD_REQUEST)

    def test_missing_fields(self) -> None:
        response = self.client.post(ENROLL_URL, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            set(response.json()), {"enrollment_key", "machine_id", "hostname"}
        )


class HeartbeatTests(DeviceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.token = self.enroll().json()["device_token"]

    def test_heartbeat_updates_device(self) -> None:
        Device.objects.update(last_seen_at=timezone.now() - timedelta(hours=1))
        self.as_device(self.token)

        response = self.client.post(HEARTBEAT_URL, {"agent_version": "0.2.0"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(body["next_heartbeat_seconds"], 60)
        self.assertEqual(body["policy_version"], 1)  # policy created lazily
        device = Device.objects.get()
        self.assertEqual(device.agent_version, "0.2.0")
        self.assertTrue(device.is_online)

    def test_bad_or_revoked_token_gets_401(self) -> None:
        self.as_device("not-a-real-token")
        response = self.client.post(HEARTBEAT_URL, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        Device.objects.update(is_revoked=True)
        self.as_device(self.token)
        response = self.client.post(HEARTBEAT_URL, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response["WWW-Authenticate"], "Device")

    def test_owner_token_cannot_heartbeat(self) -> None:
        self.as_owner()
        response = self.client.post(HEARTBEAT_URL, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_device_token_cannot_reach_account_apis(self) -> None:
        self.as_device(self.token)
        self.assertEqual(self.client.get("/api/auth/me/").status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.client.get(LIST_URL).status_code, status.HTTP_401_UNAUTHORIZED)


class OwnerTests(DeviceTestCase):
    def test_get_key_creates_one_when_missing(self) -> None:
        EnrollmentKey.objects.all().delete()
        self.as_owner()

        first = self.client.get(KEY_URL).json()["key"]
        second = self.client.get(KEY_URL).json()["key"]

        self.assertRegex(first, r"^ZNW-[2-9A-Z]{4}-[2-9A-Z]{4}-[2-9A-Z]{4}$")
        self.assertEqual(first, second)
        self.assertEqual(EnrollmentKey.objects.count(), 1)

    def test_rotate_revokes_old_key_but_devices_keep_working(self) -> None:
        token = self.enroll().json()["device_token"]
        self.as_owner()

        response = self.client.post(ROTATE_URL)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertNotEqual(response.json()["key"], self.key.key)
        self.key.refresh_from_db()
        self.assertFalse(self.key.is_active)
        self.assertIsNotNone(self.key.revoked_at)
        self.assertEqual(self.enroll().status_code, status.HTTP_400_BAD_REQUEST)

        self.as_device(token)
        self.assertEqual(self.client.post(HEARTBEAT_URL, {}, format="json").status_code, 200)

    def test_list_shows_only_own_devices(self) -> None:
        self.enroll()
        other = User.objects.create_user("other@else.in", "pw-12345678")
        other_key = EnrollmentKey.objects.create(owner=other)
        self.enroll(enrollment_key=other_key.key, hostname="OTHER-PC")
        self.as_owner()

        response = self.client.get(LIST_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([d["hostname"] for d in response.json()], ["ACCOUNTS-PC-01"])
        self.assertTrue(response.json()[0]["is_online"])
        self.assertNotIn("token_hash", response.json()[0])

    def test_owner_endpoints_require_login(self) -> None:
        for url in (KEY_URL, LIST_URL):
            self.assertEqual(self.client.get(url).status_code, status.HTTP_401_UNAUTHORIZED)


def make_event(**overrides: Any) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "type": "usb.connected",
        "occurred_at": "2026-10-09T10:15:00+05:30",
        "severity": "info",
        "data": {"device_name": "SanDisk Ultra", "drive_letter": "E:"},
        **overrides,
    }


class PolicyTests(DeviceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.token = self.enroll().json()["device_token"]

    def put_policy(self, mode: str):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.put(POLICY_URL, {"usb": {"mode": mode}}, format="json")

    def test_get_creates_default_policy(self) -> None:
        self.as_owner()

        response = self.client.get(POLICY_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["usb"], {"mode": "allow"})
        self.assertIn("updated_at", body)
        self.assertEqual(Policy.objects.count(), 1)
        self.client.get(POLICY_URL)
        self.assertEqual(Policy.objects.count(), 1)

    def test_put_bumps_version_only_on_change(self) -> None:
        self.as_owner()

        first = self.put_policy("read_only")
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(first.json()["version"], 2)
        self.assertEqual(first.json()["usb"], {"mode": "read_only"})

        same = self.put_policy("read_only")
        self.assertEqual(same.status_code, status.HTTP_200_OK)
        self.assertEqual(same.json()["version"], 2)
        self.assertEqual(same.json()["updated_at"], first.json()["updated_at"])

        self.assertEqual(self.put_policy("block").json()["version"], 3)
        self.assertEqual(self.put_policy("allow").json()["version"], 4)

        events = Event.objects.filter(name="policy.updated").order_by("id")
        self.assertEqual(
            [e.properties for e in events],
            [
                {"usb_mode": "read_only", "version": 2},
                {"usb_mode": "block", "version": 3},
                {"usb_mode": "allow", "version": 4},
            ],
        )
        self.assertEqual(events[0].actor_email, "owner@acme.in")
        self.assertEqual(events[0].ref_type, "z_devices.policy")

    def test_put_rejects_bad_mode(self) -> None:
        self.as_owner()

        response = self.put_policy("melt")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("mode", response.json()["usb"])
        self.assertEqual(self.client.put(POLICY_URL, {}, format="json").status_code, 400)
        self.assertEqual(
            self.client.put(POLICY_URL, {"usb": "block"}, format="json").status_code, 400
        )
        self.assertEqual(self.client.get(POLICY_URL).json()["version"], 1)

    def test_heartbeat_reports_current_version(self) -> None:
        self.as_owner()
        self.put_policy("block")
        self.as_device(self.token)

        body = self.client.post(HEARTBEAT_URL, {}, format="json").json()

        self.assertEqual(body["policy_version"], 2)

    def test_device_reads_its_owners_policy(self) -> None:
        self.as_owner()
        self.put_policy("read_only")
        other = User.objects.create_user("other@else.in", "pw-12345678")
        Policy.objects.create(owner=other, usb_mode="block", version=7)
        self.as_device(self.token)

        response = self.client.get(CURRENT_POLICY_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), {"version": 2, "usb": {"mode": "read_only"}})

    def test_device_policy_is_created_lazily(self) -> None:
        self.as_device(self.token)
        response = self.client.get(CURRENT_POLICY_URL)
        self.assertEqual(response.json(), {"version": 1, "usb": {"mode": "allow"}})
        self.assertEqual(Policy.objects.get().owner, self.owner)

    def test_policies_are_per_owner(self) -> None:
        other = User.objects.create_user("other@else.in", "pw-12345678")
        Policy.objects.create(owner=other, usb_mode="block", version=7)
        self.as_owner()
        self.assertEqual(self.client.get(POLICY_URL).json()["usb"]["mode"], "allow")

    def test_device_token_cannot_use_owner_policy_endpoint(self) -> None:
        self.as_device(self.token)
        self.assertEqual(self.client.get(POLICY_URL).status_code, status.HTTP_401_UNAUTHORIZED)
        response = self.client.put(POLICY_URL, {"usb": {"mode": "block"}}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertFalse(Policy.objects.filter(usb_mode="block").exists())

    def test_owner_token_cannot_use_device_policy_endpoint(self) -> None:
        self.as_owner()
        response = self.client.get(CURRENT_POLICY_URL)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_anonymous_and_revoked_are_refused(self) -> None:
        self.assertEqual(self.client.get(POLICY_URL).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(
            self.client.get(CURRENT_POLICY_URL).status_code, status.HTTP_401_UNAUTHORIZED
        )
        Device.objects.update(is_revoked=True)
        self.as_device(self.token)
        self.assertEqual(
            self.client.get(CURRENT_POLICY_URL).status_code, status.HTTP_401_UNAUTHORIZED
        )


class EventUploadTests(DeviceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.token = self.enroll().json()["device_token"]
        self.device = Device.objects.get()
        self.as_device(self.token)

    def upload(self, events: list[dict[str, Any]]):
        return self.client.post(UPLOAD_URL, {"events": events}, format="json")

    def test_upload_stores_events(self) -> None:
        event = make_event()

        response = self.upload([event, make_event(type="usb.blocked", severity="warning")])

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), {"accepted": 2, "duplicates": 0})
        stored = DeviceEvent.objects.get(client_id=event["id"])
        self.assertEqual(stored.device, self.device)
        self.assertEqual(stored.type, "usb.connected")
        self.assertEqual(stored.severity, "info")
        self.assertEqual(stored.data, event["data"])
        self.assertEqual(
            stored.occurred_at, datetime(2026, 10, 9, 4, 45, tzinfo=dt_timezone.utc)
        )
        self.assertIsNotNone(stored.received_at)

    def test_upload_is_idempotent(self) -> None:
        first, second = make_event(), make_event()
        self.upload([first])

        response = self.upload([first, second])

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), {"accepted": 1, "duplicates": 1})
        self.assertEqual(self.upload([first, second]).json(), {"accepted": 0, "duplicates": 2})
        self.assertEqual(DeviceEvent.objects.count(), 2)

    def test_uppercase_uuid_matches_stored_id(self) -> None:
        event = make_event()
        self.upload([event])
        again = {**event, "id": event["id"].upper()}
        self.assertEqual(self.upload([again]).json(), {"accepted": 0, "duplicates": 1})

    def test_repeated_id_within_a_batch_counts_as_duplicate(self) -> None:
        event = make_event()
        self.assertEqual(self.upload([event, event]).json(), {"accepted": 1, "duplicates": 1})
        self.assertEqual(DeviceEvent.objects.count(), 1)

    def test_concurrent_duplicate_is_counted_not_raised(self) -> None:
        # Simulate another request storing `first` between our lookup and our insert.
        first, second = make_event(), make_event()
        self.upload([first])
        with mock.patch("z_devices.services._stored_client_ids", return_value=set()):
            response = self.upload([first, second])

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), {"accepted": 1, "duplicates": 1})
        self.assertEqual(DeviceEvent.objects.count(), 2)

    def test_same_client_id_on_two_devices_is_not_a_duplicate(self) -> None:
        event = make_event()
        self.upload([event])
        other = self.enroll(machine_id="second-machine", hostname="FRONT-DESK").json()
        self.as_device(other["device_token"])

        self.assertEqual(self.upload([event]).json(), {"accepted": 1, "duplicates": 0})
        self.assertEqual(DeviceEvent.objects.count(), 2)

    def test_batch_size_limits(self) -> None:
        self.assertEqual(self.upload([]).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            self.client.post(UPLOAD_URL, {}, format="json").status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        too_many = [make_event() for _ in range(101)]
        self.assertEqual(self.upload(too_many).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(DeviceEvent.objects.exists())

        self.assertEqual(self.upload(too_many[:100]).json(), {"accepted": 100, "duplicates": 0})

    def test_type_must_match_pattern(self) -> None:
        bad_types = ("usb", "USB.connected", "usb.", ".usb", "usb..x", "usb-x.y", "usb.x1", "")
        for bad in bad_types:
            with self.subTest(type=bad):
                response = self.upload([make_event(type=bad)])
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("type", response.json()["events"]["0"])
        for good in ("usb.connected", "usb.policy_applied", "file_ops.copy.large"):
            with self.subTest(type=good):
                self.assertEqual(self.upload([make_event(type=good)]).status_code, 200)

    def test_data_must_be_a_small_object(self) -> None:
        self.assertEqual(self.upload([make_event(data=["x"])]).status_code, 400)
        self.assertEqual(self.upload([make_event(data="x")]).status_code, 400)

        overhead = len('{"blob":""}')
        just_fits = make_event(data={"blob": "a" * (4096 - overhead)})
        too_big = make_event(data={"blob": "a" * (4097 - overhead)})
        self.assertEqual(self.upload([just_fits]).status_code, 200)
        response = self.upload([too_big])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("data", response.json()["events"]["0"])

    def test_one_bad_event_rejects_the_whole_batch(self) -> None:
        response = self.upload([make_event(), make_event(severity="meh")])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("severity", response.json()["events"]["1"])
        self.assertFalse(DeviceEvent.objects.exists())

    def test_required_fields_and_defaults(self) -> None:
        response = self.upload([{"type": "usb.connected"}])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.json()["events"]["0"]), {"id", "occurred_at"})

        minimal = {
            "id": str(uuid.uuid4()),
            "type": "usb.connected",
            "occurred_at": "2026-10-09T05:00:00Z",
        }
        self.assertEqual(self.upload([minimal]).status_code, 200)
        stored = DeviceEvent.objects.get()
        self.assertEqual((stored.severity, stored.data), ("info", {}))

    def test_naive_timestamp_is_read_as_utc(self) -> None:
        self.upload([make_event(occurred_at="2026-10-09T05:00:00")])
        self.assertEqual(
            DeviceEvent.objects.get().occurred_at,
            datetime(2026, 10, 9, 5, 0, tzinfo=dt_timezone.utc),
        )

    def test_owner_token_cannot_upload(self) -> None:
        self.as_owner()
        self.assertEqual(self.upload([make_event()]).status_code, status.HTTP_401_UNAUTHORIZED)
        self.client.credentials()
        self.assertEqual(self.upload([make_event()]).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertFalse(DeviceEvent.objects.exists())

    def test_revoked_device_cannot_upload(self) -> None:
        Device.objects.update(is_revoked=True)
        response = self.upload([make_event()])
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response["WWW-Authenticate"], "Device")
        self.assertFalse(DeviceEvent.objects.exists())


class EventFeedTests(DeviceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.token = self.enroll().json()["device_token"]
        self.device = Device.objects.get()
        second_id = self.enroll(machine_id="second", hostname="FRONT-DESK").json()["device_id"]
        self.second = Device.objects.get(pk=second_id)
        self.base = datetime(2026, 10, 9, 4, 0, tzinfo=dt_timezone.utc)

    def add(self, device: Device, minutes: int, **fields: Any) -> DeviceEvent:
        return DeviceEvent.objects.create(
            device=device,
            client_id=uuid.uuid4(),
            type=fields.pop("type", "usb.connected"),
            occurred_at=self.base + timedelta(minutes=minutes),
            data=fields.pop("data", {"device_name": "SanDisk Ultra"}),
            **fields,
        )

    def feed(self, **params: Any):
        return self.client.get(EVENTS_URL, params)

    def test_feed_shape_and_order(self) -> None:
        old = self.add(self.device, 1)
        new = self.add(self.second, 30, type="usb.blocked", severity="warning")
        middle = self.add(
            self.device, 10, type="usb.policy_applied", data={"mode": "read_only", "dry_run": True}
        )
        self.as_owner()

        response = self.feed()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = response.json()
        self.assertEqual([r["id"] for r in rows], [new.id, middle.id, old.id])
        self.assertEqual(
            set(rows[0]),
            {"id", "device_id", "hostname", "type", "severity", "title",
             "occurred_at", "received_at", "data"},
        )
        self.assertEqual(rows[0]["device_id"], self.second.id)
        self.assertEqual(rows[0]["hostname"], "FRONT-DESK")
        self.assertEqual(rows[0]["type"], "usb.blocked")
        self.assertEqual(rows[0]["severity"], "warning")
        self.assertEqual(rows[0]["title"], 'Blocked USB drive "SanDisk Ultra" on FRONT-DESK')
        self.assertEqual(
            rows[1]["title"], 'USB rule "Read-only" is now active on ACCOUNTS-PC-01 (test mode)'
        )
        self.assertEqual(rows[2]["title"], 'USB drive "SanDisk Ultra" plugged into ACCOUNTS-PC-01')
        self.assertEqual(rows[2]["data"], {"device_name": "SanDisk Ultra"})

    def test_ordering_is_by_occurred_at_not_upload_order(self) -> None:
        later = self.add(self.device, 50)
        earlier_but_uploaded_last = self.add(self.device, 5)
        self.as_owner()
        ids = [r["id"] for r in self.feed().json()]
        self.assertEqual(ids, [later.id, earlier_but_uploaded_last.id])

    def test_feed_shows_only_own_devices(self) -> None:
        mine = self.add(self.device, 1)
        other = User.objects.create_user("other@else.in", "pw-12345678")
        other_key = EnrollmentKey.objects.create(owner=other)
        other_id = self.enroll(enrollment_key=other_key.key, machine_id="theirs").json()["device_id"]
        theirs = self.add(Device.objects.get(pk=other_id), 5)
        self.as_owner()

        self.assertEqual([r["id"] for r in self.feed().json()], [mine.id])
        # Filtering by someone else's device returns nothing rather than their events.
        response = self.feed(device=theirs.device_id)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), [])

    def test_device_filter(self) -> None:
        mine = self.add(self.device, 1)
        self.add(self.second, 2)
        self.as_owner()

        self.assertEqual([r["id"] for r in self.feed(device=self.device.id).json()], [mine.id])
        self.assertEqual(len(self.feed().json()), 2)
        self.assertEqual(self.feed(device="abc").status_code, status.HTTP_400_BAD_REQUEST)

    def test_limit(self) -> None:
        for minute in range(60):
            self.add(self.device, minute)
        self.as_owner()

        default = self.feed().json()
        self.assertEqual(len(default), 50)
        rows = self.feed(limit=3).json()
        self.assertEqual([r["id"] for r in rows], [r["id"] for r in default[:3]])
        self.assertEqual(len(self.feed(limit=200).json()), 60)
        for bad in (0, 201, -1, "lots"):
            with self.subTest(limit=bad):
                response = self.feed(limit=bad)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("limit", response.json())

    def test_revoked_devices_history_stays_visible(self) -> None:
        self.add(self.device, 1)
        Device.objects.update(is_revoked=True)
        self.as_owner()
        self.assertEqual(len(self.feed().json()), 1)

    def test_empty_feed(self) -> None:
        self.as_owner()
        response = self.feed()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), [])

    def test_feed_requires_owner_auth(self) -> None:
        self.assertEqual(self.feed().status_code, status.HTTP_401_UNAUTHORIZED)
        self.as_device(self.token)
        self.assertEqual(self.feed().status_code, status.HTTP_401_UNAUTHORIZED)


class TitleTests(APITestCase):
    HOST = "ACCOUNTS-PC-01"

    def test_known_types(self) -> None:
        cases = [
            ("usb.connected", {"device_name": "SanDisk Ultra", "drive_letter": "E:"},
             'USB drive "SanDisk Ultra" plugged into ACCOUNTS-PC-01'),
            ("usb.connected", {}, "A USB drive was plugged into ACCOUNTS-PC-01"),
            ("usb.blocked", {"device_name": "SanDisk Ultra"},
             'Blocked USB drive "SanDisk Ultra" on ACCOUNTS-PC-01'),
            ("usb.blocked", {"device_name": 42}, "Blocked a USB drive on ACCOUNTS-PC-01"),
            ("usb.policy_applied", {"mode": "read_only", "dry_run": False},
             'USB rule "Read-only" is now active on ACCOUNTS-PC-01'),
            ("usb.policy_applied", {"mode": "block", "dry_run": True},
             'USB rule "Block" is now active on ACCOUNTS-PC-01 (test mode)'),
            ("usb.policy_applied", {"mode": "allow"},
             'USB rule "Allow" is now active on ACCOUNTS-PC-01'),
            ("usb.policy_applied", {"mode": "weird", "dry_run": "yes"},
             "A new USB rule is now active on ACCOUNTS-PC-01"),
        ]
        for event_type, data, expected in cases:
            with self.subTest(type=event_type, data=data):
                self.assertEqual(build_title(event_type, data, self.HOST), expected)

    def test_unknown_type_gets_generic_sentence(self) -> None:
        self.assertEqual(
            build_title("file.copied_to_usb", {"x": 1}, self.HOST),
            'ACCOUNTS-PC-01 reported a "file copied to usb" event',
        )

    def test_untrusted_values_are_tidied(self) -> None:
        name = "  Big\n\tDisk  " + "x" * 200
        title = build_title("usb.connected", {"device_name": name}, self.HOST)
        self.assertTrue(title.startswith('USB drive "Big Disk xxx'))
        self.assertIn("…", title)
        self.assertLess(len(title), 140)
        self.assertEqual(build_title("usb.connected", None, ""), "A USB drive was plugged into a PC")
