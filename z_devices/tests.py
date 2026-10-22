from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from typing import Any
from unittest import mock

from django.conf import settings
from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from z_analytics.models import Event
from z_auth._testing import give_plan
from z_auth.entitlements import entitlements_for
from z_auth.models import User
from z_notification.models import Notification

from .events import build_title
from .models import (
    Device,
    DeviceEvent,
    EnrollmentKey,
    Member,
    Message,
    Policy,
    UsageMetric,
    hash_device_token,
)

ENROLL_URL = "/api/devices/enroll/"
HEARTBEAT_URL = "/api/devices/heartbeat/"
KEY_URL = "/api/devices/enrollment-key/"
ROTATE_URL = "/api/devices/enrollment-key/rotate/"
LIST_URL = "/api/devices/"
MEMBERS_URL = "/api/devices/members/"
POLICY_URL = "/api/devices/policy/"
CURRENT_POLICY_URL = "/api/devices/policy/current/"
EVENTS_URL = "/api/devices/events/"
UPLOAD_URL = "/api/devices/events/upload/"
MESSAGES_URL = "/api/devices/messages/"
METRICS_URL = "/api/devices/metrics/"
METRICS_UPLOAD_URL = "/api/devices/metrics/upload/"

HEARTBEAT_RESPONSE_KEYS = {
    "device_id", "server_time", "next_heartbeat_seconds", "policy_version", "consent_state", "usb_restore",
    "company_name", "owner_name",
}

# The non-usb feature blocks as a freshly created policy emits them (see contract defaults).
DEFAULT_WEB = {"enabled": True, "mode": "monitor", "categories": [], "custom_domains": []}
DEFAULT_DATA_TRANSFER = {
    "enabled": True,
    "usb_warn_bytes": 52428800,
    "lan_warn_bytes": 52428800,
    "email_warn_bytes": 20971520,
    "include_details": False,
    "usb_lockdown": False,
}
DEFAULT_REMOTE_SOFTWARE = {
    "enabled": True,
    "blocking_enabled": False,
    "blocked_tools": [],
    "extra_tool_executables": [],
}


def default_policy_blocks() -> dict[str, Any]:
    """Every non-usb block at its creation default, for exact-shape assertions."""
    return {
        "web": dict(DEFAULT_WEB),
        "data_transfer": dict(DEFAULT_DATA_TRANSFER),
        "remote_software": dict(DEFAULT_REMOTE_SOFTWARE),
    }


class DeviceTestCase(APITestCase):
    def setUp(self) -> None:
        cache.clear()  # reset throttle counters between tests
        self.owner = User.objects.create_user("owner@acme.in", "pw-12345678")
        # The feature tests run on the top plan; plan limits have their own tests (test_plan_limits).
        give_plan(self.owner, "businesses")
        self.member = Member.objects.create(
            owner=self.owner, name="Asha Rao", email="asha@acme.in"
        )

    def enroll(self, **overrides: Any):
        payload = {
            "email": self.member.email,
            "member_id": self.member.member_code,
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

    def agent_version(self, saved_version: int) -> int:
        """What the agent is told for a saved policy version (the plan's module set is folded in)."""
        return saved_version * 1000 + entitlements_for(self.owner).fingerprint

    def agent_policy(self, saved_version: int, usb_mode: str) -> dict[str, Any]:
        """The full policy a Businesses-plan PC receives (nothing clamped)."""
        return {
            "version": self.agent_version(saved_version),
            "usb": {"mode": usb_mode},
            **default_policy_blocks(),
            "entitlements": sorted(entitlements_for(self.owner).modules),
            "max_devices": 50,
            "consent_state": "pending",
            "monitoring_enabled": True,
            "company_name": "",
            "owner_name": "",
        }


class EnrollTests(DeviceTestCase):
    def test_enroll_creates_device_and_returns_token_once(self) -> None:
        response = self.enroll()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()
        device = Device.objects.get()
        self.assertEqual(body["device_id"], device.id)
        self.assertFalse(body["re_enrolled"])
        self.assertEqual(device.owner, self.owner)
        self.assertEqual(device.member, self.member)
        self.assertEqual(device.machine_id, "6f1c2a40-0000-4000-8000-abcdef012345")
        # Only the hash is stored.
        self.assertEqual(device.token_hash, hash_device_token(body["device_token"]))
        self.assertNotIn(body["device_token"], device.token_hash)
        self.assertEqual(Event.objects.get().name, "device.enrolled")

    def test_member_id_is_case_and_whitespace_insensitive(self) -> None:
        response = self.enroll(
            member_id=f"  {self.member.member_code.lower()} ",
            email=f"  {self.member.email.upper()} ",
        )
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

    def test_unknown_member_id_is_rejected_on_member_id_field(self) -> None:
        response = self.enroll(member_id="ZM-AAAA-BBBB")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(list(response.json()), ["member_id"])
        self.assertFalse(Device.objects.exists())

    def test_wrong_email_is_rejected_on_email_field(self) -> None:
        response = self.enroll(email="someone-else@acme.in")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(list(response.json()), ["email"])
        self.assertFalse(Device.objects.exists())

    def test_inactive_member_is_rejected_on_non_field_errors(self) -> None:
        Member.objects.filter(pk=self.member.pk).update(is_active=False)
        response = self.enroll()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(list(response.json()), ["non_field_errors"])
        self.assertFalse(Device.objects.exists())

    def test_disabled_owner_is_rejected_on_non_field_errors(self) -> None:
        User.objects.filter(pk=self.owner.pk).update(is_active=False)
        response = self.enroll()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(list(response.json()), ["non_field_errors"])

    def test_missing_fields(self) -> None:
        response = self.client.post(ENROLL_URL, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            set(response.json()), {"email", "member_id", "machine_id", "hostname"}
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
        self.assertEqual(body["policy_version"], self.agent_version(1))  # policy created lazily
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
    def enroll_other_owner(self, hostname: str = "OTHER-PC"):
        """Enrol a PC for a different owner's member (for isolation assertions)."""
        other = User.objects.create_user("other@else.in", "pw-12345678")
        other_member = Member.objects.create(
            owner=other, name="Rafi Khan", email="rafi@else.in"
        )
        return self.enroll(
            email=other_member.email,
            member_id=other_member.member_code,
            machine_id="other-machine",
            hostname=hostname,
        )

    def test_list_shows_only_own_devices(self) -> None:
        self.enroll()
        self.enroll_other_owner()
        self.as_owner()

        response = self.client.get(LIST_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([d["hostname"] for d in response.json()], ["ACCOUNTS-PC-01"])
        self.assertTrue(response.json()[0]["is_online"])
        self.assertEqual(response.json()[0]["member_name"], "Asha Rao")
        self.assertNotIn("token_hash", response.json()[0])

    def test_owner_endpoints_require_login(self) -> None:
        for url in (MEMBERS_URL, LIST_URL):
            self.assertEqual(self.client.get(url).status_code, status.HTTP_401_UNAUTHORIZED)


class EnrollmentKeyEndpointTests(DeviceTestCase):
    """The legacy org-key endpoints stay (back-compat) even though enrol no longer uses them."""

    def test_get_key_creates_one_when_missing(self) -> None:
        EnrollmentKey.objects.all().delete()
        self.as_owner()

        first = self.client.get(KEY_URL).json()["key"]
        second = self.client.get(KEY_URL).json()["key"]

        self.assertRegex(first, r"^ZNW-[2-9A-Z]{4}-[2-9A-Z]{4}-[2-9A-Z]{4}$")
        self.assertEqual(first, second)
        self.assertEqual(EnrollmentKey.objects.count(), 1)

    def test_rotate_revokes_old_key_and_issues_a_new_one(self) -> None:
        key = EnrollmentKey.objects.create(owner=self.owner)
        self.as_owner()

        response = self.client.post(ROTATE_URL)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertNotEqual(response.json()["key"], key.key)
        key.refresh_from_db()
        self.assertFalse(key.is_active)
        self.assertIsNotNone(key.revoked_at)


class MemberTests(DeviceTestCase):
    def test_create_member_returns_id_and_persists(self) -> None:
        self.as_owner()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                MEMBERS_URL, {"name": "Bina Das", "email": "Bina@Acme.in"}, format="json"
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()
        self.assertRegex(body["member_id"], r"^ZM-[2-9A-Z]{4}-[2-9A-Z]{4}$")
        self.assertTrue(body["is_active"])
        self.assertIsNone(body["device"])
        self.assertFalse(body["online"])
        member = Member.objects.get(email="bina@acme.in")  # stored lowercased
        self.assertEqual(member.owner, self.owner)
        self.assertEqual(member.name, "Bina Das")
        self.assertEqual(Event.objects.filter(name="member.created").count(), 1)

    def test_create_member_emails_the_member_their_id_and_download_link(self) -> None:
        self.as_owner()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                MEMBERS_URL, {"name": "Bina Das", "email": "bina@acme.in"}, format="json"
            )
        code = response.json()["member_id"]

        sent = [m for m in mail.outbox if "bina@acme.in" in m.to]
        self.assertEqual(len(sent), 1)
        invite = sent[0]
        self.assertIn(code, invite.body)                 # the member gets their id
        self.assertIn("bina@acme.in", invite.body)        # and their email
        self.assertIn("/download", invite.body)           # and the download link
        # The id is a secret: the logged copy is redacted, the sent copy is not.
        note = Notification.objects.get(template="member_invite", recipient="bina@acme.in")
        self.assertNotIn(code, note.body)

    def test_duplicate_email_for_same_owner_is_rejected(self) -> None:
        self.as_owner()
        response = self.client.post(
            MEMBERS_URL, {"name": "Asha Again", "email": "ASHA@acme.in"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("email", response.json())
        self.assertEqual(self.owner.members.count(), 1)

    def test_same_email_allowed_for_different_owners(self) -> None:
        other = User.objects.create_user("boss@else.in", "pw-12345678")
        Member.objects.create(owner=other, name="Asha Rao", email="asha@acme.in")
        # self.owner already has asha@acme.in; the other owner having it too is fine.
        self.assertEqual(Member.objects.filter(email="asha@acme.in").count(), 2)

    def test_list_members_includes_device_and_online(self) -> None:
        self.enroll()
        self.as_owner()
        response = self.client.get(MEMBERS_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row = response.json()[0]
        self.assertEqual(row["email"], "asha@acme.in")
        self.assertEqual(row["member_id"], self.member.member_code)
        self.assertTrue(row["online"])
        self.assertEqual(row["device"]["hostname"], "ACCOUNTS-PC-01")
        self.assertTrue(row["device"]["is_online"])
        self.assertEqual(row["download_url"], settings.ZINWE_AGENT_DOWNLOAD_URL)
        self.assertEqual(
            set(row),
            {
                "id", "name", "email", "member_id", "is_active", "created_at",
                "online", "device", "download_url", "unread",
            },
        )

    def test_member_shows_online_after_a_heartbeat(self) -> None:
        token = self.enroll().json()["device_token"]
        Device.objects.update(last_seen_at=timezone.now() - timedelta(hours=1))
        self.as_owner()
        self.assertFalse(self.client.get(MEMBERS_URL).json()[0]["online"])

        self.as_device(token)
        self.assertEqual(
            self.client.post(HEARTBEAT_URL, {}, format="json").status_code, status.HTTP_200_OK
        )
        self.as_owner()
        self.assertTrue(self.client.get(MEMBERS_URL).json()[0]["online"])

    def test_rotate_id_invalidates_the_old_id(self) -> None:
        old_code = self.member.member_code
        self.as_owner()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"{MEMBERS_URL}{self.member.id}/rotate-id/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        new_code = response.json()["member_id"]
        self.assertNotEqual(new_code, old_code)

        # Enrolling with the old id now fails; the new id works.
        self.client.credentials()
        self.assertEqual(
            self.enroll(member_id=old_code).status_code, status.HTTP_400_BAD_REQUEST
        )
        self.assertEqual(
            self.enroll(member_id=new_code).status_code, status.HTTP_201_CREATED
        )

    def test_deactivate_revokes_devices_and_blocks_enroll(self) -> None:
        token = self.enroll().json()["device_token"]
        self.as_owner()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"{MEMBERS_URL}{self.member.id}/deactivate/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.json()["is_active"])
        device = Device.objects.get()
        self.assertTrue(device.is_revoked)

        # The revoked device's token is dead, and re-enrol is refused.
        self.as_device(token)
        self.assertEqual(
            self.client.post(HEARTBEAT_URL, {}, format="json").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        self.client.credentials()
        response = self.enroll()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("non_field_errors", response.json())

    def test_cannot_touch_another_owners_member(self) -> None:
        other = User.objects.create_user("boss@else.in", "pw-12345678")
        theirs = Member.objects.create(owner=other, name="Rafi", email="rafi@else.in")
        self.as_owner()
        self.assertEqual(
            self.client.post(f"{MEMBERS_URL}{theirs.id}/rotate-id/").status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            self.client.post(f"{MEMBERS_URL}{theirs.id}/deactivate/").status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_member_endpoints_require_owner_auth(self) -> None:
        self.as_device(self.enroll().json()["device_token"])
        self.assertEqual(self.client.get(MEMBERS_URL).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(
            self.client.post(MEMBERS_URL, {"name": "x", "email": "x@y.in"}, format="json").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )


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
        self.assertEqual(body["web"], DEFAULT_WEB)
        self.assertEqual(body["data_transfer"], DEFAULT_DATA_TRANSFER)
        self.assertEqual(body["remote_software"], DEFAULT_REMOTE_SOFTWARE)
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

    def test_put_accepts_a_partial_block_and_ignores_unknown_ones(self) -> None:
        self.as_owner()

        # A block the server doesn't know yet (a future feature) is ignored, so a
        # PUT carrying only unknown blocks changes nothing and is rejected as empty.
        unknown = self.client.put(
            POLICY_URL, {"screenshots": {"enabled": True}}, format="json"
        )
        self.assertEqual(unknown.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.client.get(POLICY_URL).json()["version"], 1)

        # A known block still applies on its own.
        self.assertEqual(self.put_policy("block").json()["version"], 2)

    def test_heartbeat_reports_current_version(self) -> None:
        self.as_owner()
        self.put_policy("block")
        self.as_device(self.token)

        body = self.client.post(HEARTBEAT_URL, {}, format="json").json()

        self.assertEqual(body["policy_version"], self.agent_version(2))

    def test_device_reads_its_owners_policy(self) -> None:
        self.as_owner()
        self.put_policy("read_only")
        other = User.objects.create_user("other@else.in", "pw-12345678")
        Policy.objects.create(owner=other, usb_mode="block", version=7)
        self.as_device(self.token)

        response = self.client.get(CURRENT_POLICY_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.json(),
            self.agent_policy(2, "read_only"),
        )
        # Device GET is the agent's wire contract: no owner-only `updated_at`.
        self.assertNotIn("updated_at", response.json())

    def test_device_policy_is_created_lazily(self) -> None:
        self.as_device(self.token)
        response = self.client.get(CURRENT_POLICY_URL)
        self.assertEqual(
            response.json(),
            self.agent_policy(1, "allow"),
        )
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


class MultiBlockPolicyTests(DeviceTestCase):
    """The web / data_transfer / remote_software feature blocks (M5 configure)."""

    def setUp(self) -> None:
        super().setUp()
        self.token = self.enroll().json()["device_token"]

    def put(self, body: dict[str, Any]):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.put(POLICY_URL, body, format="json")

    # -- per-block PUTs --------------------------------------------------------

    def test_web_block_applies(self) -> None:
        self.as_owner()
        body = {
            "web": {
                "enabled": True,
                "mode": "block",
                "categories": ["adult", "malware"],
                "custom_domains": ["bad.example", "evil.test"],
            }
        }
        response = self.put(body)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        result = response.json()
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["web"], body["web"])
        policy = Policy.objects.get(owner=self.owner)
        self.assertEqual(policy.web_mode, "block")
        self.assertEqual(policy.web_categories, ["adult", "malware"])

    def test_data_transfer_block_applies(self) -> None:
        self.as_owner()
        body = {
            "data_transfer": {
                "enabled": True,
                "usb_warn_bytes": 1048576,
                "lan_warn_bytes": 2097152,
                "email_warn_bytes": 0,
                "include_details": True,
                "usb_lockdown": True,
            }
        }
        response = self.put(body)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["version"], 2)
        self.assertEqual(response.json()["data_transfer"], body["data_transfer"])
        self.assertTrue(Policy.objects.get().data_transfer_usb_lockdown)

    def test_remote_software_block_applies(self) -> None:
        self.as_owner()
        body = {
            "remote_software": {
                "enabled": True,
                "blocking_enabled": True,
                "blocked_tools": ["AnyDesk", "TeamViewer"],
                "extra_tool_executables": ["myrat"],
            }
        }
        response = self.put(body)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["version"], 2)
        self.assertEqual(response.json()["remote_software"], body["remote_software"])

    # -- partial multi-block + single version bump -----------------------------

    def test_multi_block_put_bumps_version_once(self) -> None:
        self.as_owner()
        body = {
            "usb": {"mode": "block"},
            "web": {
                "enabled": False,
                "mode": "monitor",
                "categories": [],
                "custom_domains": [],
            },
            "remote_software": {
                "enabled": True,
                "blocking_enabled": True,
                "blocked_tools": ["AnyDesk"],
                "extra_tool_executables": [],
            },
        }
        response = self.put(body)

        self.assertEqual(response.json()["version"], 2)  # one bump for three blocks
        # The one policy.updated track() carries every changed model field at once.
        event = Event.objects.get(name="policy.updated")
        self.assertEqual(
            event.properties,
            {
                "usb_mode": "block",
                "web_enabled": False,
                "remote_software_blocking_enabled": True,
                "remote_software_blocked_tools": ["AnyDesk"],
                "version": 2,
            },
        )

    def test_partial_change_within_a_block_tracks_only_changed_fields(self) -> None:
        self.as_owner()
        # Only include_details differs from defaults; the other data_transfer fields match.
        body = {
            "data_transfer": {
                "enabled": True,
                "usb_warn_bytes": 52428800,
                "lan_warn_bytes": 52428800,
                "email_warn_bytes": 20971520,
                "include_details": True,
            }
        }
        response = self.put(body)
        self.assertEqual(response.json()["version"], 2)
        event = Event.objects.get(name="policy.updated")
        self.assertEqual(
            event.properties, {"data_transfer_include_details": True, "version": 2}
        )

    def test_noop_put_does_not_bump_or_track(self) -> None:
        self.as_owner()
        # Re-send the current defaults for every block: nothing changes.
        body = {
            "usb": {"mode": "allow"},
            "web": DEFAULT_WEB,
            "data_transfer": DEFAULT_DATA_TRANSFER,
            "remote_software": DEFAULT_REMOTE_SOFTWARE,
        }
        response = self.put(body)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["version"], 1)
        self.assertFalse(Event.objects.filter(name="policy.updated").exists())

    # -- validation ------------------------------------------------------------

    def test_rejects_bad_web_mode(self) -> None:
        self.as_owner()
        response = self.client.put(
            POLICY_URL,
            {"web": {"enabled": True, "mode": "melt", "categories": [], "custom_domains": []}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("mode", response.json()["web"])
        self.assertEqual(self.client.get(POLICY_URL).json()["version"], 1)

    def test_rejects_negative_warn_bytes(self) -> None:
        self.as_owner()
        response = self.client.put(
            POLICY_URL,
            {
                "data_transfer": {
                    "enabled": True,
                    "usb_warn_bytes": -1,
                    "lan_warn_bytes": 52428800,
                    "email_warn_bytes": 20971520,
                    "include_details": False,
                }
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("usb_warn_bytes", response.json()["data_transfer"])

    def test_rejects_non_string_list_item(self) -> None:
        self.as_owner()
        response = self.client.put(
            POLICY_URL,
            {
                "remote_software": {
                    "enabled": True,
                    "blocking_enabled": True,
                    "blocked_tools": ["AnyDesk", 42],
                    "extra_tool_executables": [],
                }
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("blocked_tools", response.json()["remote_software"])

    def test_rejects_missing_required_field_in_block(self) -> None:
        self.as_owner()
        # `web` present but incomplete (no enabled/categories/custom_domains).
        response = self.client.put(POLICY_URL, {"web": {"mode": "block"}}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.client.get(POLICY_URL).json()["version"], 1)

    # -- auth: device vs owner on the owner endpoint ---------------------------

    def test_device_token_cannot_configure_blocks(self) -> None:
        self.as_device(self.token)
        response = self.client.put(
            POLICY_URL,
            {"web": {"enabled": True, "mode": "block", "categories": [], "custom_domains": []}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertFalse(Policy.objects.filter(web_mode="block").exists())


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
             "occurred_at", "received_at", "data", "emailed_at", "kind", "handled_at", "handled_by"},
        )
        self.assertIsNone(rows[0]["emailed_at"])
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
        other_member = Member.objects.create(owner=other, name="Rafi", email="rafi@else.in")
        other_id = self.enroll(
            email=other_member.email, member_id=other_member.member_code, machine_id="theirs"
        ).json()["device_id"]
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


MESSAGES_READ_URL = "/api/devices/messages/read/"


class ChatThreadTests(DeviceTestCase):
    """Two-way chat: member replies, owner thread, unread badges, seen state."""

    def setUp(self) -> None:
        super().setUp()
        self.token = self.enroll().json()["device_token"]

    def _send(self, title: str = "USB blocked") -> None:
        self.as_owner()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(f"{MEMBERS_URL}{self.member.id}/message/", {"title": title}, format="json")

    def _member_row(self):  # type: ignore[no-untyped-def]
        self.as_owner()
        return next(m for m in self.client.get(MEMBERS_URL).json() if m["id"] == self.member.id)

    def test_member_reply_threads_and_shows_unread_to_admin(self) -> None:
        self._send()
        self.as_device(self.token)
        with self.captureOnCommitCallbacks(execute=True):
            reply = self.client.post(MESSAGES_URL, {"body": "Why? I need it for work."}, format="json")
        self.assertEqual(reply.status_code, status.HTTP_201_CREATED)
        self.assertEqual(reply.json()["sender"], "member")
        self.assertTrue(reply.json()["read_by_member"])
        self.assertFalse(reply.json()["read_by_owner"])

        # The member's own feed shows both sides of the conversation.
        feed = self.client.get(MESSAGES_URL).json()
        self.assertEqual({m["sender"] for m in feed}, {"owner", "member"})

        # The admin's Team list flags one unread reply...
        self.assertEqual(self._member_row()["unread"], 1)
        # ...until the admin opens the thread (which returns both lines and marks them read).
        thread = self.client.get(f"{MEMBERS_URL}{self.member.id}/messages/").json()
        self.assertEqual(len(thread), 2)
        self.assertEqual(self._member_row()["unread"], 0)

    def test_member_read_marks_admin_messages_seen(self) -> None:
        self._send()
        self.as_device(self.token)
        self.client.post(MESSAGES_READ_URL)
        message = Message.objects.get(sender="owner")
        self.assertTrue(message.read_by_member)
        self.assertIsNotNone(message.read_by_member_at)
        self.assertIsNotNone(message.delivered_at)  # read implies delivered, even if never polled

    def _thread(self) -> list[dict]:
        self.as_owner()
        return self.client.get(f"{MEMBERS_URL}{self.member.id}/messages/").json()

    def test_receipts_go_sent_then_delivered_then_seen(self) -> None:
        self._send()
        sent = self._thread()[0]
        self.assertIsNone(sent["delivered_at"])
        self.assertIsNone(sent["read_by_member_at"])

        # The PC pulls the conversation: delivered, not yet read.
        self.as_device(self.token)
        self.client.get(MESSAGES_URL)
        delivered = self._thread()[0]
        self.assertIsNotNone(delivered["delivered_at"])
        self.assertFalse(delivered["read_by_member"])

        # The member opens the chat: the agent reports it read.
        self.as_device(self.token)
        self.client.post(MESSAGES_READ_URL)
        seen = self._thread()[0]
        self.assertTrue(seen["read_by_member"])
        self.assertIsNotNone(seen["read_by_member_at"])
        self.assertEqual(seen["delivered_at"], delivered["delivered_at"])  # first delivery time kept

    def test_polling_again_keeps_the_first_delivery_time_and_skips_replies(self) -> None:
        self._send()
        self.as_device(self.token)
        self.client.get(MESSAGES_URL)
        first = Message.objects.get(sender="owner").delivered_at
        self.client.post(MESSAGES_URL, {"body": "ok"}, format="json")
        self.client.get(MESSAGES_URL)
        self.assertEqual(Message.objects.get(sender="owner").delivered_at, first)
        self.assertIsNone(Message.objects.get(sender="member").delivered_at)  # receipts are for the admin's lines

    def test_empty_reply_rejected(self) -> None:
        self.as_device(self.token)
        self.assertEqual(
            self.client.post(MESSAGES_URL, {"body": "   "}, format="json").status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_thread_requires_owner_auth(self) -> None:
        self.as_device(self.token)
        self.assertEqual(
            self.client.get(f"{MEMBERS_URL}{self.member.id}/messages/").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )


class MessageTests(DeviceTestCase):
    """Owner -> member messages, and the agent's bare-array poll."""

    def setUp(self) -> None:
        super().setUp()
        self.token = self.enroll().json()["device_token"]

    def message_url(self, member: Member | None = None) -> str:
        return f"{MEMBERS_URL}{(member or self.member).id}/message/"

    def send(self, body: dict[str, Any], member: Member | None = None):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(self.message_url(member), body, format="json")

    def test_owner_sends_and_device_receives(self) -> None:
        self.as_owner()
        response = self.send(
            {"title": "USB storage is now blocked", "body": "Ask IT if you need it.",
             "severity": "warning"}
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        created = response.json()
        self.assertEqual(
            set(created),
            {"id", "sender", "channel", "title", "body", "severity", "created_at", "read_by_owner", "read_by_member",
             "delivered_at", "read_by_member_at"},
        )
        self.assertIsNone(created["delivered_at"])  # not on the PC yet
        self.assertEqual(created["sender"], "owner")
        self.assertTrue(created["read_by_owner"])
        self.assertFalse(created["read_by_member"])
        self.assertEqual(created["title"], "USB storage is now blocked")
        self.assertEqual(created["body"], "Ask IT if you need it.")
        self.assertEqual(created["severity"], "warning")
        message = Message.objects.get()
        self.assertEqual(message.owner, self.owner)
        self.assertEqual(message.member, self.member)
        self.assertEqual(created["id"], message.id)

        # It was tracked on commit.
        event = Event.objects.get(name="member.message_sent")
        self.assertEqual(event.properties, {"member_id": self.member.id, "severity": "warning", "channel": "chat"})
        self.assertEqual(event.actor_email, "owner@acme.in")
        self.assertEqual(event.ref_type, "z_devices.message")

        # The agent's poll returns it as a bare array.
        self.as_device(self.token)
        feed = self.client.get(MESSAGES_URL)
        self.assertEqual(feed.status_code, status.HTTP_200_OK)
        [line] = feed.json()
        # Pulling it is what delivers it; everything else is the line the owner created.
        self.assertIsNotNone(line.pop("delivered_at"))
        created.pop("delivered_at")
        self.assertEqual(line, created)

    def test_body_defaults_null_and_severity_defaults_info(self) -> None:
        self.as_owner()
        created = self.send({"title": "Please restart your PC"}).json()
        self.assertIsNone(created["body"])
        self.assertEqual(created["severity"], "info")
        self.assertEqual(Message.objects.get().body, "")  # stored as "", served as null

    def test_device_feed_is_newest_first(self) -> None:
        self.as_owner()
        self.send({"title": "First"})
        self.send({"title": "Second"})
        self.send({"title": "Third"})

        self.as_device(self.token)
        titles = [m["title"] for m in self.client.get(MESSAGES_URL).json()]
        self.assertEqual(titles, ["Third", "Second", "First"])

    def test_feed_caps_at_fifty(self) -> None:
        self.as_owner()
        now = timezone.now()
        Message.objects.bulk_create(
            [
                Message(owner=self.owner, member=self.member, title=f"m{i}", severity="info")
                for i in range(55)
            ]
        )
        # bulk_create with auto_now_add leaves created_at equal-ish; order by -id is the tiebreak.
        Message.objects.update(created_at=now)
        self.as_device(self.token)
        rows = self.client.get(MESSAGES_URL).json()
        self.assertEqual(len(rows), 50)

    def test_cannot_message_another_owners_member(self) -> None:
        other = User.objects.create_user("boss@else.in", "pw-12345678")
        theirs = Member.objects.create(owner=other, name="Rafi", email="rafi@else.in")
        self.as_owner()
        response = self.send({"title": "hi"}, member=theirs)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(Message.objects.exists())

    def test_device_only_sees_its_own_members_messages(self) -> None:
        # A message for our member, and one for a different member on another device.
        self.as_owner()
        self.send({"title": "For Asha"})

        other_member = Member.objects.create(
            owner=self.owner, name="Bina", email="bina@acme.in"
        )
        other_token = self.enroll(
            email=other_member.email,
            member_id=other_member.member_code,
            machine_id="bina-machine",
            hostname="BINA-PC",
        ).json()["device_token"]
        self.send({"title": "For Bina"}, member=other_member)

        self.as_device(self.token)
        self.assertEqual(
            [m["title"] for m in self.client.get(MESSAGES_URL).json()], ["For Asha"]
        )
        self.as_device(other_token)
        self.assertEqual(
            [m["title"] for m in self.client.get(MESSAGES_URL).json()], ["For Bina"]
        )

    def test_device_with_no_member_gets_empty_array(self) -> None:
        Device.objects.update(member=None)
        self.as_device(self.token)
        response = self.client.get(MESSAGES_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), [])

    def test_feed_requires_device_auth(self) -> None:
        # Anonymous.
        self.assertEqual(
            self.client.get(MESSAGES_URL).status_code, status.HTTP_401_UNAUTHORIZED
        )
        # Owner token is not a device.
        self.as_owner()
        self.assertEqual(
            self.client.get(MESSAGES_URL).status_code, status.HTTP_401_UNAUTHORIZED
        )
        # Revoked device.
        Device.objects.update(is_revoked=True)
        self.as_device(self.token)
        self.assertEqual(
            self.client.get(MESSAGES_URL).status_code, status.HTTP_401_UNAUTHORIZED
        )

    def test_send_requires_owner_auth(self) -> None:
        self.assertEqual(
            self.client.post(self.message_url(), {"title": "x"}, format="json").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        self.as_device(self.token)
        self.assertEqual(
            self.client.post(self.message_url(), {"title": "x"}, format="json").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertFalse(Message.objects.exists())

    def test_missing_title_is_rejected(self) -> None:
        self.as_owner()
        response = self.client.post(self.message_url(), {"body": "no title"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("title", response.json())
        self.assertFalse(Message.objects.exists())

    def test_blank_and_overlong_title_are_rejected(self) -> None:
        self.as_owner()
        self.assertEqual(
            self.client.post(self.message_url(), {"title": "   "}, format="json").status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.client.post(
                self.message_url(), {"title": "x" * 121}, format="json"
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertFalse(Message.objects.exists())

    def test_bad_severity_is_rejected(self) -> None:
        self.as_owner()
        response = self.client.post(
            self.message_url(), {"title": "hi", "severity": "meh"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("severity", response.json())
        self.assertFalse(Message.objects.exists())


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


class HeartbeatWorkStateTests(DeviceTestCase):
    """M6: the heartbeat now carries the member's work status; the response is unchanged."""

    def setUp(self) -> None:
        super().setUp()
        self.token = self.enroll().json()["device_token"]
        self.as_device(self.token)

    def test_heartbeat_stores_work_state(self) -> None:
        before = timezone.now()
        response = self.client.post(
            HEARTBEAT_URL,
            {"work_state": "working", "worked_seconds": 3600, "break_seconds": 300},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # The response shape must stay exactly the four contract keys.
        self.assertEqual(set(response.json()), HEARTBEAT_RESPONSE_KEYS)

        device = Device.objects.get()
        self.assertEqual(device.work_state, "working")
        self.assertEqual(device.worked_seconds, 3600)
        self.assertEqual(device.break_seconds, 300)
        self.assertIsNotNone(device.work_updated_at)
        self.assertGreaterEqual(device.work_updated_at, before)

    def test_on_break_state_is_stored(self) -> None:
        self.client.post(HEARTBEAT_URL, {"work_state": "on_break"}, format="json")
        self.assertEqual(Device.objects.get().work_state, "on_break")

    def test_bad_work_state_is_rejected(self) -> None:
        response = self.client.post(HEARTBEAT_URL, {"work_state": "napping"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("work_state", response.json())

    def test_old_heartbeat_without_work_fields_still_works(self) -> None:
        # An older agent omits the work fields entirely: it must still heartbeat,
        # with the unchanged four-key response.
        response = self.client.post(HEARTBEAT_URL, {"agent_version": "0.1.0"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(set(response.json()), HEARTBEAT_RESPONSE_KEYS)
        device = Device.objects.get()
        self.assertEqual(device.work_state, "not_started")  # sane default
        self.assertEqual(device.worked_seconds, 0)


class ForcedUpgradeTests(DeviceTestCase):
    """M12: an agent below ZINWE_AGENT_MIN_VERSION is told to update on heartbeat."""

    def setUp(self) -> None:
        super().setUp()
        self.token = self.enroll(agent_version="0.1.0").json()["device_token"]
        self.as_device(self.token)

    def _beat(self, agent_version: str):
        return self.client.post(HEARTBEAT_URL, {"agent_version": agent_version}, format="json")

    @override_settings(ZINWE_AGENT_MIN_VERSION="0.2.0", ZINWE_AGENT_DOWNLOAD_URL="https://zinwe.io/get")
    def test_old_agent_is_told_to_update(self) -> None:
        body = self._beat("0.1.0").json()
        self.assertTrue(body["update_required"])
        self.assertEqual(body["download_url"], "https://zinwe.io/get")

    @override_settings(ZINWE_AGENT_MIN_VERSION="0.2.0")
    def test_current_agent_is_not_told_to_update(self) -> None:
        # At or above the minimum: the keys are omitted, so the shape is unchanged.
        self.assertEqual(set(self._beat("0.2.0").json()), HEARTBEAT_RESPONSE_KEYS)
        self.assertEqual(set(self._beat("1.0.0").json()), HEARTBEAT_RESPONSE_KEYS)

    def test_no_minimum_means_no_forced_update(self) -> None:
        # Default settings have an empty minimum, so even an ancient agent is fine.
        self.assertEqual(set(self._beat("0.0.1").json()), HEARTBEAT_RESPONSE_KEYS)


class MetricsUploadTests(DeviceTestCase):
    """M6: POST /api/devices/metrics/upload/ -- hour-bucketed usage, REPLACE semantics."""

    BUCKET = datetime(2026, 10, 10, 9, 0, tzinfo=dt_timezone.utc)

    def setUp(self) -> None:
        super().setUp()
        self.token = self.enroll().json()["device_token"]
        self.as_device(self.token)

    def _row(self, **overrides: Any) -> dict[str, Any]:
        row = {
            "bucket_start": self.BUCKET.isoformat(),
            "category": "web.domain",
            "key": "youtube.com",
            "count": 3,
            "bytes": 1024,
            "seconds": 120,
        }
        row.update(overrides)
        return row

    def upload(self, rows: list[dict[str, Any]]):
        return self.client.post(METRICS_UPLOAD_URL, {"metrics": rows}, format="json")

    def test_upload_inserts_rows(self) -> None:
        response = self.upload([
            self._row(),
            self._row(category="transfer.usb", key="", count=1, bytes=5_000_000, seconds=0),
        ])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), {"accepted": 2})
        self.assertEqual(UsageMetric.objects.count(), 2)
        web = UsageMetric.objects.get(category="web.domain", key="youtube.com")
        self.assertEqual((web.count, web.bytes, web.seconds), (3, 1024, 120))
        usb = UsageMetric.objects.get(category="transfer.usb", key="")
        self.assertEqual(usb.bytes, 5_000_000)

    def test_resend_replaces_not_adds(self) -> None:
        self.upload([self._row(count=3, bytes=1024, seconds=120)])
        # The agent re-sends the still-open current hour with GROWN totals.
        self.upload([self._row(count=7, bytes=4096, seconds=300)])

        self.assertEqual(UsageMetric.objects.count(), 1)  # same bucket/category/key
        row = UsageMetric.objects.get()
        # REPLACE, not add: the values are the latest upload's, not 3+7 etc.
        self.assertEqual((row.count, row.bytes, row.seconds), (7, 4096, 300))

    def test_distinct_keys_and_buckets_are_separate_rows(self) -> None:
        other_bucket = datetime(2026, 10, 10, 10, 0, tzinfo=dt_timezone.utc)
        self.upload([
            self._row(),
            self._row(key="vimeo.com"),
            self._row(bucket_start=other_bucket.isoformat()),
        ])
        self.assertEqual(UsageMetric.objects.count(), 3)

    def test_empty_batch_rejected(self) -> None:
        response = self.upload([])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(UsageMetric.objects.exists())

    def test_too_many_rows_rejected(self) -> None:
        rows = [self._row(key=f"d{i}.com") for i in range(1001)]
        response = self.upload(rows)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(UsageMetric.objects.exists())

    def test_exactly_1000_rows_accepted(self) -> None:
        rows = [self._row(key=f"d{i}.com") for i in range(1000)]
        response = self.upload(rows)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["accepted"], 1000)

    def test_blank_category_rejected(self) -> None:
        response = self.upload([self._row(category="")])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_negative_count_rejected(self) -> None:
        response = self.upload([self._row(count=-1)])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_owner_token_cannot_upload(self) -> None:
        self.as_owner()
        response = self.upload([self._row()])
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_unauthenticated_cannot_upload(self) -> None:
        self.client.credentials()
        response = self.upload([self._row()])
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class MetricsOwnerReadTests(DeviceTestCase):
    """M6: the owner reads aggregated usage for the dashboard charts."""

    # Two hours inside the default 7-day window, whatever day the suite runs (fixed dates fell out of it).
    H9 = (timezone.now() - timedelta(days=2)).replace(hour=9, minute=0, second=0, microsecond=0)
    H10 = H9 + timedelta(hours=1)

    def setUp(self) -> None:
        super().setUp()
        self.token = self.enroll().json()["device_token"]
        self.device = Device.objects.get()

    def _metric(self, device: Device, bucket: datetime, category: str, key: str,
                count: int = 0, byts: int = 0, seconds: int = 0) -> None:
        UsageMetric.objects.create(
            device=device, bucket_start=bucket, category=category, key=key,
            count=count, bytes=byts, seconds=seconds,
        )

    def test_device_token_cannot_read_owner_metrics(self) -> None:
        self.as_device(self.token)
        self.assertEqual(self.client.get(METRICS_URL).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_owner_login_required(self) -> None:
        self.assertEqual(self.client.get(METRICS_URL).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_returns_only_own_data_with_totals_and_series(self) -> None:
        self._metric(self.device, self.H9, "web.domain", "youtube.com", count=3, byts=100, seconds=60)
        self._metric(self.device, self.H10, "web.domain", "youtube.com", count=2, byts=50, seconds=30)
        self._metric(self.device, self.H9, "transfer.usb", "", count=1, byts=5000, seconds=0)

        # Another owner's device + metric must never leak in.
        other = User.objects.create_user("other@else.in", "pw-12345678")
        other_member = Member.objects.create(owner=other, name="Rafi", email="rafi@else.in")
        other_device = Device.objects.create(
            owner=other, member=other_member, machine_id="m2", hostname="OTHER",
            token_hash="x" * 64,
        )
        self._metric(other_device, self.H9, "web.domain", "evil.com", count=999, byts=9, seconds=9)

        self.as_owner()
        body = self.client.get(METRICS_URL).json()

        cats = {c["category"]: c for c in body["categories"]}
        self.assertEqual(set(cats), {"web.domain", "transfer.usb"})
        self.assertEqual(cats["web.domain"]["total_count"], 5)   # 3 + 2, own data only
        self.assertEqual(cats["web.domain"]["total_bytes"], 150)
        self.assertEqual(cats["web.domain"]["total_seconds"], 90)
        self.assertEqual(cats["transfer.usb"]["total_bytes"], 5000)
        self.assertNotIn("evil.com", str(body))

        # Series is per (bucket, category), oldest bucket first.
        buckets = [s["bucket_start"] for s in body["series"]]
        self.assertEqual(buckets, sorted(buckets))
        self.assertEqual(len(body["series"]), 3)

    def test_category_filter(self) -> None:
        self._metric(self.device, self.H9, "web.domain", "youtube.com", count=3)
        self._metric(self.device, self.H9, "transfer.usb", "", count=1)
        self.as_owner()
        body = self.client.get(METRICS_URL, {"category": "transfer.usb"}).json()
        self.assertEqual([c["category"] for c in body["categories"]], ["transfer.usb"])
        self.assertTrue(all(s["category"] == "transfer.usb" for s in body["series"]))

    def test_member_filter(self) -> None:
        # A second member + PC under the same owner; filtering to one member
        # excludes the other's rows.
        member2 = Member.objects.create(owner=self.owner, name="Bina", email="bina@acme.in")
        device2 = Device.objects.create(
            owner=self.owner, member=member2, machine_id="m2", hostname="PC2",
            token_hash="y" * 64,
        )
        self._metric(self.device, self.H9, "web.domain", "a.com", count=3)
        self._metric(device2, self.H9, "web.domain", "b.com", count=10)

        self.as_owner()
        body = self.client.get(METRICS_URL, {"member": member2.id}).json()
        self.assertEqual(body["categories"][0]["total_count"], 10)
        self.assertEqual(len(body["series"]), 1)

    def test_days_filter(self) -> None:
        recent = timezone.now() - timedelta(days=2)
        stale = timezone.now() - timedelta(days=20)
        self._metric(self.device, recent, "web.domain", "a.com", count=3)
        self._metric(self.device, stale, "web.domain", "a.com", count=99)

        self.as_owner()
        body = self.client.get(METRICS_URL, {"days": 7}).json()
        self.assertEqual(body["categories"][0]["total_count"], 3)  # stale excluded
        body_all = self.client.get(METRICS_URL, {"days": 31}).json()
        self.assertEqual(body_all["categories"][0]["total_count"], 102)

    def test_invalid_days_rejected(self) -> None:
        self.as_owner()
        self.assertEqual(
            self.client.get(METRICS_URL, {"days": 0}).status_code, status.HTTP_400_BAD_REQUEST
        )
        self.assertEqual(
            self.client.get(METRICS_URL, {"days": 99}).status_code, status.HTTP_400_BAD_REQUEST
        )


METRICS_TOP_URL = "/api/devices/metrics/top/"


class MetricsTopReadTests(DeviceTestCase):
    """Per-key breakdown (top apps / top websites) for the owner's own devices only."""

    # Two hours inside the default 7-day window, whatever day the suite runs (fixed dates fell out of it).
    H9 = (timezone.now() - timedelta(days=2)).replace(hour=9, minute=0, second=0, microsecond=0)
    H10 = H9 + timedelta(hours=1)

    def setUp(self) -> None:
        super().setUp()
        self.token = self.enroll().json()["device_token"]
        self.device = Device.objects.get()

    def _metric(self, device: Device, bucket: datetime, category: str, key: str,
                count: int = 0, seconds: int = 0) -> None:
        UsageMetric.objects.create(
            device=device, bucket_start=bucket, category=category, key=key,
            count=count, seconds=seconds,
        )

    def test_category_required(self) -> None:
        self.as_owner()
        self.assertEqual(self.client.get(METRICS_TOP_URL).status_code, status.HTTP_400_BAD_REQUEST)

    def test_owner_login_required(self) -> None:
        self.assertEqual(
            self.client.get(METRICS_TOP_URL, {"category": "web.domain"}).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_ranks_sites_by_time_and_sums_across_hours(self) -> None:
        self._metric(self.device, self.H9, "web.domain", "youtube.com", count=3, seconds=120)
        self._metric(self.device, self.H10, "web.domain", "youtube.com", count=2, seconds=60)
        self._metric(self.device, self.H9, "web.domain", "github.com", count=10, seconds=600)
        # Another owner's row must never leak in.
        other = User.objects.create_user("other@else.in", "pw-12345678")
        od = Device.objects.create(owner=other, machine_id="m2", hostname="O", token_hash="x" * 64)
        self._metric(od, self.H9, "web.domain", "evil.com", count=999, seconds=9999)

        self.as_owner()
        body = self.client.get(METRICS_TOP_URL, {"category": "web.domain"}).json()
        self.assertEqual(body["category"], "web.domain")
        keys = [i["key"] for i in body["items"]]
        self.assertEqual(keys, ["github.com", "youtube.com"])  # github more seconds; no leak
        yt = next(i for i in body["items"] if i["key"] == "youtube.com")
        self.assertEqual(yt["seconds"], 180)  # 120 + 60 summed across hours
        self.assertEqual(yt["count"], 5)
        self.assertNotIn("evil.com", str(body))

    def test_blank_keys_excluded(self) -> None:
        self._metric(self.device, self.H9, "transfer.usb", "", count=1, seconds=0)
        self.as_owner()
        body = self.client.get(METRICS_TOP_URL, {"category": "transfer.usb"}).json()
        self.assertEqual(body["items"], [])

    def test_limit(self) -> None:
        for n in range(5):
            self._metric(self.device, self.H9, "app.foreground", f"app{n}", count=1, seconds=n + 1)
        self.as_owner()
        body = self.client.get(METRICS_TOP_URL, {"category": "app.foreground", "limit": 2}).json()
        self.assertEqual(len(body["items"]), 2)
        self.assertEqual(body["items"][0]["key"], "app4")  # highest seconds first

    def test_byte_category_ranks_by_bytes(self) -> None:
        # A size-based category (seconds = 0) ranks by bytes, not alphabetically.
        self._metric(self.device, self.H9, "net.upload", "aaa.example.com", count=1, seconds=0)
        UsageMetric.objects.filter(device=self.device, key="aaa.example.com").update(bytes=10)
        self._metric(self.device, self.H9, "net.upload", "zzz.example.com", count=1, seconds=0)
        UsageMetric.objects.filter(device=self.device, key="zzz.example.com").update(bytes=9_000_000)
        self.as_owner()
        body = self.client.get(METRICS_TOP_URL, {"category": "net.upload"}).json()
        self.assertEqual([i["key"] for i in body["items"]], ["zzz.example.com", "aaa.example.com"])


class WorkStateOnOwnerReadsTests(DeviceTestCase):
    """M6: work_state is exposed on the device list and the members list's device."""

    def setUp(self) -> None:
        super().setUp()
        self.token = self.enroll().json()["device_token"]
        self.as_device(self.token)
        self.client.post(
            HEARTBEAT_URL,
            {"work_state": "working", "worked_seconds": 1800, "break_seconds": 60},
            format="json",
        )

    def test_device_list_shows_work_state(self) -> None:
        self.as_owner()
        row = self.client.get(LIST_URL).json()[0]
        self.assertEqual(row["work_state"], "working")
        self.assertEqual(row["worked_seconds"], 1800)
        self.assertEqual(row["break_seconds"], 60)

    def test_members_list_shows_work_state_on_device(self) -> None:
        self.as_owner()
        row = self.client.get(MEMBERS_URL).json()[0]
        self.assertEqual(row["device"]["work_state"], "working")
        self.assertEqual(row["device"]["worked_seconds"], 1800)
        self.assertEqual(row["device"]["break_seconds"], 60)


class DataUsedMetricsTests(DeviceTestCase):
    """FR-04: internet data used per app / site arrives from the agent and ranks by bytes."""

    def setUp(self) -> None:
        super().setUp()
        self.token = self.enroll().json()["device_token"]

    def test_upload_then_top_ranks_by_data(self) -> None:
        from z_auth import entitlements

        hour = (timezone.now() - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0).isoformat()
        rows = [
            {"bucket_start": hour, "category": "web.data.app", "key": "chrome", "count": 1, "bytes": 3_000_000_000},
            {"bucket_start": hour, "category": "web.data.app", "key": "whatsapp", "count": 1, "bytes": 50_000_000},
            {"bucket_start": hour, "category": "web.data.site", "key": "googlevideo.com", "count": 1, "bytes": 2_500_000_000},
        ]
        self.as_device(self.token)
        response = self.client.post(METRICS_UPLOAD_URL, {"metrics": rows}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)

        self.as_owner()
        apps = self.client.get(METRICS_TOP_URL, {"category": "web.data.app"}).json()["items"]
        self.assertEqual([i["key"] for i in apps], ["chrome", "whatsapp"])
        self.assertEqual(apps[0]["bytes"], 3_000_000_000)  # over 2 GB in one hour fits
        sites = self.client.get(METRICS_TOP_URL, {"category": "web.data.site"}).json()["items"]
        self.assertEqual(sites[0]["key"], "googlevideo.com")
        # Plan-gated with Online protection (web).
        self.assertEqual(entitlements.module_for("web.data.app"), entitlements.WEB)
