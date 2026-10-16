"""Plan enforcement: the PC limit, the policy a PC is sent, kept/hidden data and locked endpoints."""
from __future__ import annotations

import uuid
from datetime import datetime
from datetime import timezone as dt_timezone
from typing import Any

from django.utils import timezone
from rest_framework import status

from z_auth._testing import give_plan
from z_auth.entitlements import entitlements_for
from z_auth.models import Subscription

from .models import Device, DeviceEvent, Policy, UsageMetric, WorkLog
from .services import UNREACHABLE_BYTES
from .tests import (
    CURRENT_POLICY_URL,
    EVENTS_URL,
    HEARTBEAT_URL,
    MEMBERS_URL,
    MESSAGES_URL,
    METRICS_UPLOAD_URL,
    METRICS_URL,
    POLICY_URL,
    UPLOAD_URL,
    DeviceTestCase,
    make_event,
)

WORK_LOG_URL = "/api/devices/work-log/"
ME_URL = "/api/auth/me/"


class PlanTestCase(DeviceTestCase):
    """DeviceTestCase starts on Businesses; `use_plan(None)` drops to Free forever (no active plan)."""

    def use_plan(self, code: str | None) -> None:
        Subscription.objects.filter(owner=self.owner).delete()
        if code is not None:
            give_plan(self.owner, code)


class PcLimitTests(PlanTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.use_plan(None)  # Free forever: 3 PCs

    def test_new_pc_over_the_limit_is_refused(self) -> None:
        for n in range(3):
            self.assertEqual(self.enroll(machine_id=f"pc-{n}").status_code, status.HTTP_201_CREATED)

        response = self.enroll(machine_id="pc-4")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("covers 3 PCs", response.json()["non_field_errors"][0])
        self.assertEqual(Device.objects.count(), 3)

    def test_an_enrolled_pc_can_always_re_enrol(self) -> None:
        for n in range(3):
            self.enroll(machine_id=f"pc-{n}")
        self.assertEqual(self.enroll(machine_id="pc-0").status_code, status.HTTP_200_OK)

    def test_a_removed_pc_frees_its_slot_but_needs_one_to_come_back(self) -> None:
        for n in range(3):
            self.enroll(machine_id=f"pc-{n}")
        Device.objects.filter(machine_id="pc-0").update(is_revoked=True)
        self.assertEqual(self.enroll(machine_id="pc-new").status_code, status.HTTP_201_CREATED)
        # pc-0 coming back would make four.
        self.assertEqual(self.enroll(machine_id="pc-0").status_code, status.HTTP_400_BAD_REQUEST)

    def test_upgrading_raises_the_limit(self) -> None:
        for n in range(3):
            self.enroll(machine_id=f"pc-{n}")
        self.use_plan("startups")
        self.assertEqual(self.enroll(machine_id="pc-4").status_code, status.HTTP_201_CREATED)

    def test_a_downgrade_never_cuts_off_working_pcs(self) -> None:
        self.use_plan("startups")
        for n in range(5):
            self.enroll(machine_id=f"pc-{n}")
        self.use_plan(None)
        self.assertEqual(self.enroll(machine_id="pc-2").status_code, status.HTTP_200_OK)
        self.assertEqual(Device.objects.filter(is_revoked=False).count(), 5)


class EffectivePolicyTests(PlanTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.token = self.enroll().json()["device_token"]
        # Rules saved while on a full plan.
        Policy.objects.create(
            owner=self.owner, usb_mode="block", web_mode="block", data_transfer_usb_lockdown=True,
            remote_software_blocking_enabled=True,
        )

    def agent_policy(self) -> dict[str, Any]:
        self.as_device(self.token)
        return self.client.get(CURRENT_POLICY_URL).json()

    def test_free_plan_pc_gets_usb_alerts_only(self) -> None:
        self.use_plan(None)
        body = self.agent_policy()
        self.assertEqual(body["usb"], {"mode": "allow"})
        self.assertTrue(body["data_transfer"]["enabled"])  # USB copy alerts still on
        self.assertFalse(body["data_transfer"]["usb_lockdown"])
        self.assertEqual(body["data_transfer"]["lan_warn_bytes"], UNREACHABLE_BYTES)
        self.assertEqual(body["data_transfer"]["email_warn_bytes"], UNREACHABLE_BYTES)
        self.assertFalse(body["remote_software"]["enabled"])
        self.assertFalse(body["remote_software"]["blocking_enabled"])
        self.assertFalse(body["web"]["enabled"])
        self.assertEqual(body["entitlements"], ["usb_alerts"])
        self.assertEqual(body["max_devices"], 3)

    def test_startups_pc_gets_the_saved_rules(self) -> None:
        self.use_plan("startups")
        body = self.agent_policy()
        self.assertEqual(body["usb"], {"mode": "block"})
        self.assertTrue(body["data_transfer"]["usb_lockdown"])
        self.assertTrue(body["remote_software"]["blocking_enabled"])
        self.assertEqual(body["web"]["mode"], "block")
        self.assertTrue(body["web"]["enabled"])

    def test_a_plan_change_makes_pcs_refetch(self) -> None:
        self.use_plan(None)
        self.as_device(self.token)
        before = self.client.post(HEARTBEAT_URL, {}, format="json").json()["policy_version"]
        self.use_plan("startups")
        after = self.client.post(HEARTBEAT_URL, {}, format="json").json()["policy_version"]
        self.assertNotEqual(before, after)
        # ...and the heartbeat and the policy agree, so the agent stops re-fetching.
        self.assertEqual(after, self.client.get(CURRENT_POLICY_URL).json()["version"])

    def test_the_saved_rules_are_untouched(self) -> None:
        self.use_plan(None)
        self.agent_policy()
        self.assertEqual(Policy.objects.get(owner=self.owner).usb_mode, "block")


class PolicyEditTests(PlanTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.use_plan(None)
        self.as_owner()

    def put(self, body: dict[str, Any]):  # type: ignore[no-untyped-def]
        return self.client.put(POLICY_URL, body, format="json")

    def test_blocking_usb_needs_startups(self) -> None:
        response = self.put({"usb": {"mode": "block"}})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        body = response.json()
        self.assertEqual(body["code"], "plan_feature")
        self.assertEqual(body["module"], "usb_control")
        self.assertEqual(body["upgrade_plan"], "startups")
        self.assertIn("Startups", body["detail"])

    def test_allowing_usb_is_fine_on_free(self) -> None:
        self.assertEqual(self.put({"usb": {"mode": "allow"}}).status_code, status.HTTP_200_OK)

    def test_web_rules_need_startups(self) -> None:
        response = self.put({"web": {"enabled": True, "mode": "block", "categories": [], "custom_domains": []}})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.json()["module"], "web")

    def test_startups_can_block_usb(self) -> None:
        self.use_plan("startups")
        self.assertEqual(self.put({"usb": {"mode": "block"}}).status_code, status.HTTP_200_OK)


class DataTests(PlanTestCase):
    BUCKET = datetime(2026, 10, 10, 9, 0, tzinfo=dt_timezone.utc)

    def setUp(self) -> None:
        super().setUp()
        self.token = self.enroll().json()["device_token"]
        self.device = Device.objects.get()

    def test_locked_events_are_handled_but_not_kept(self) -> None:
        self.use_plan(None)
        self.as_device(self.token)
        response = self.client.post(
            UPLOAD_URL,
            {"events": [make_event(), make_event(type="remote.tool_detected"), make_event(type="web.blocked_attempt")]},
            format="json",
        )
        self.assertEqual(response.json(), {"accepted": 3, "duplicates": 0})
        self.assertEqual(list(DeviceEvent.objects.values_list("type", flat=True)), ["usb.connected"])

    def test_feed_hides_events_from_before_a_downgrade(self) -> None:
        for kind in ("usb.connected", "remote.tool_detected"):
            DeviceEvent.objects.create(
                device=self.device, client_id=uuid.uuid4(), type=kind, occurred_at=timezone.now(), data={}
            )
        self.use_plan(None)
        self.as_owner()
        types = [row["type"] for row in self.client.get(EVENTS_URL).json()]
        self.assertEqual(types, ["usb.connected"])

    def test_app_usage_is_a_businesses_metric(self) -> None:
        self.use_plan("startups")
        self.as_device(self.token)
        rows = [
            {"bucket_start": self.BUCKET.isoformat(), "category": category, "key": key, "count": 1, "seconds": 60}
            for category, key in (("web.domain", "zinwe.io"), ("app.foreground", "winword"))
        ]
        self.assertEqual(self.client.post(METRICS_UPLOAD_URL, {"metrics": rows}, format="json").json(), {"accepted": 2})
        self.assertEqual(list(UsageMetric.objects.values_list("category", flat=True)), ["web.domain"])

    def test_metrics_read_hides_locked_categories(self) -> None:
        for category in ("transfer.usb", "transfer.lan"):
            UsageMetric.objects.create(
                device=self.device, bucket_start=timezone.now(), category=category, key="", count=1, bytes=10
            )
        self.use_plan(None)
        self.as_owner()
        categories = [row["category"] for row in self.client.get(METRICS_URL).json()["categories"]]
        self.assertEqual(categories, ["transfer.usb"])

    def test_no_work_log_without_work_hours(self) -> None:
        self.use_plan("startups")
        self.as_device(self.token)
        self.client.post(HEARTBEAT_URL, {"work_state": "working", "worked_seconds": 600, "break_seconds": 0}, format="json")
        self.assertFalse(WorkLog.objects.exists())
        device = Device.objects.get()
        self.assertEqual((device.work_state, device.worked_seconds), ("not_started", 0))


class LockedEndpointTests(PlanTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.token = self.enroll().json()["device_token"]
        self.device = Device.objects.get()
        self.use_plan("startups")

    def test_work_log_is_businesses_only(self) -> None:
        self.as_owner()
        response = self.client.get(WORK_LOG_URL)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.json()["module"], "work_time")
        self.assertEqual(response.json()["upgrade_plan_name"], "Businesses")

    def test_chat_is_businesses_only(self) -> None:
        self.as_owner()
        response = self.client.post(
            f"{MEMBERS_URL}{self.member.id}/message/", {"title": "Hi", "body": "Hello"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.get(f"{MEMBERS_URL}{self.member.id}/messages/").status_code, 403)
        # The PC sees an empty chat and can't reply.
        self.as_device(self.token)
        self.assertEqual(self.client.get(MESSAGES_URL).json(), [])
        self.assertEqual(
            self.client.post(MESSAGES_URL, {"body": "hi"}, format="json").status_code, status.HTTP_403_FORBIDDEN
        )

    def test_alert_to_employee_is_businesses_only(self) -> None:
        event = DeviceEvent.objects.create(
            device=self.device, client_id=uuid.uuid4(), type="usb.blocked", occurred_at=timezone.now(), data={}
        )
        self.as_owner()
        response = self.client.post(f"{EVENTS_URL}{event.id}/email/", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.json()["module"], "alert_to_employee")


class MeEntitlementsTests(PlanTestCase):
    def test_businesses_owner_sees_every_module_and_their_pc_count(self) -> None:
        self.enroll()
        self.as_owner()
        body = self.client.get(ME_URL).json()["entitlements"]
        self.assertEqual(body["plan_code"], "businesses")
        self.assertTrue(body["on_active_plan"])
        self.assertEqual(set(body["modules"]), set(entitlements_for(self.owner).modules))
        self.assertEqual(body["max_devices"], 50)
        self.assertEqual(body["devices_used"], 1)
        self.assertEqual(body["upgrade_for"], {})

    def test_a_paid_plan_waiting_for_payment_counts_as_free(self) -> None:
        self.use_plan(None)
        give_plan(self.owner, "startups")
        Subscription.objects.filter(owner=self.owner).update(status=Subscription.Status.PENDING_PAYMENT)
        self.as_owner()
        self.assertEqual(self.client.get(ME_URL).json()["entitlements"]["plan_code"], "free")
