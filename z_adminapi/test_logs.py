"""FR-37 Logs: the merged timeline API, its status card, resolving failures, `ops_event` and the 500 handler."""
from __future__ import annotations

import logging
import tempfile
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import CommandError, call_command
from django.http import HttpRequest
from django.test import override_settings
from django.urls import path
from django.utils import timezone
from rest_framework import status

from z_analytics.error_log import OpsErrorHandler
from z_analytics.models import Event, OpsEvent
from z_analytics.ops import environment, record, release_info
from z_auth.models import ManualPayment, Plan, Subscription
from z_devices.models import AgentRelease

from .tests import SUBS, AdminApiTestCase

LOGS = "/api/admin/logs/"
STATUS = "/api/admin/logs/status/"


def ops(type_: str = "deploy", what: str = "Shipped to dev", result: str = "ok", ago: timedelta | None = None, **kw):  # type: ignore[no-untyped-def]
    at = timezone.now() - (ago or timedelta(minutes=5))
    return record(type_, kw.pop("label", ""), what, result=result, env=kw.pop("env", "local"), at=at, **kw)


class LogsPermissionTests(AdminApiTestCase):
    def test_staff_only(self) -> None:
        event = ops("backup", "Nightly backup", "fail")
        for method, url in (("get", LOGS), ("get", STATUS), ("post", f"{LOGS}ops/{event.pk}/resolve/")):
            self.as_user(self.make_owner(f"{method}{len(url)}@acme.in"))
            self.assertEqual(getattr(self.client, method)(url).status_code, status.HTTP_403_FORBIDDEN, url)
            self.client.credentials()
            self.assertEqual(getattr(self.client, method)(url).status_code, status.HTTP_401_UNAUTHORIZED, url)
        event.refresh_from_db()
        self.assertTrue(event.unresolved)


class LogsMergeTests(AdminApiTestCase):
    def test_staff_actions_and_ops_events_in_one_timeline(self) -> None:
        ops("deploy", "Promoted to prod · 20261030-0900", label="Promote", ago=timedelta(hours=2))
        owner = self.make_owner("gone@acme.in")
        self.assertEqual(self.client.delete(f"{SUBS}{owner.pk}/").status_code, status.HTTP_204_NO_CONTENT)

        body = self.client.get(LOGS).json()
        self.assertEqual(body["env"], "local")
        self.assertEqual(body["range"], "7d")
        whats = [r["what"] for r in body["rows"]]
        self.assertEqual(whats, ["Deleted customer gone@acme.in", "Promoted to prod · 20261030-0900"])  # newest first
        staff, deploy = body["rows"]
        self.assertEqual((staff["type"], staff["label"], staff["who"], staff["source"]), ("staff", "Staff action", "ops@zinwe.local", "audit"))
        self.assertIn(["Source", "Admin audit log"], staff["details"])
        self.assertEqual((deploy["type"], deploy["label"], deploy["who"], deploy["source"]), ("deploy", "Promote", "system", "ops"))
        self.assertEqual(body["counts"]["all"], 2)
        self.assertEqual((body["counts"]["staff"], body["counts"]["deploy"], body["counts"]["backup"]), (1, 1, 0))

    def test_staff_rows_read_like_sentences(self) -> None:
        created = self.client.post("/api/admin/announcements/", {"title": "Maintenance tonight"}, format="json").json()
        self.client.patch(f"/api/admin/announcements/{created['id']}/", {"is_active": False}, format="json")
        rows = [r["what"] for r in self.client.get(LOGS).json()["rows"]]
        self.assertEqual(rows, ["Edited announcement · Maintenance tonight", "Published announcement · Maintenance tonight"])

    def test_agent_rows_from_releases_and_their_edits(self) -> None:
        release = AgentRelease.objects.create(version="1.4.0", sha256="a" * 64, published_by=self.staff, required=False)
        Event.objects.create(name="agent.release_published", properties={"version": "1.4.0", "required": False},
                             actor_email="ops@zinwe.local", ref_type="z_devices.agentrelease", ref_id=str(release.pk))
        # Published a minute ago (Windows clocks tick in ~15 ms: same-instant rows would tie).
        AgentRelease.objects.filter(pk=release.pk).update(published_at=timezone.now() - timedelta(minutes=1))
        self.client.patch(f"/api/admin/releases/{release.pk}/", {"required": True}, format="json")
        body = self.client.get(LOGS, {"type": "agent"}).json()
        self.assertEqual([r["what"] for r in body["rows"]],
                         ["Changed Windows agent 1.4.0 · required/optional", "Published Windows agent 1.4.0 · Optional"])
        self.assertTrue(all(r["type"] == "agent" and r["who"] == "ops@zinwe.local" for r in body["rows"]))
        self.assertEqual(body["counts"]["staff"], 0)

    def test_payment_reviews_are_staff_rows(self) -> None:
        owner = self.make_owner("pay@acme.in")
        sub = Subscription.objects.create(owner=owner, plan=Plan.objects.get(code="startups"))
        ManualPayment.objects.create(
            subscription=sub, method="upi", amount_inr="1146.00", paid_on=timezone.localdate(), payer_name="Acme",
            status=ManualPayment.Status.VERIFIED, reviewed_at=timezone.now(), reviewed_by=self.staff, staff_reference="UTR1",
        )
        ManualPayment.objects.create(  # still waiting: not a staff action yet
            subscription=sub, method="upi", amount_inr="10.00", paid_on=timezone.localdate(), payer_name="Acme",
        )
        rows = self.client.get(LOGS, {"q": "payment"}).json()["rows"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["what"].startswith("Approved payment · pay@acme.in · ₹1,146"), rows[0]["what"])
        self.assertEqual(rows[0]["who"], "ops@zinwe.local")
        self.assertIn(["Reference checked", "UTR1"], rows[0]["details"])

    def test_non_admin_events_stay_out(self) -> None:
        Event.objects.create(name="auth.login", actor_email="owner@acme.in")
        self.assertEqual(self.client.get(LOGS).json()["rows"], [])


class LogsFilterTests(AdminApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        ops("deploy", "Shipped to dev · backend", ago=timedelta(hours=1), details=[["Backend", "f3a9d21 Log backup size"]])
        ops("backup", "Nightly backup · off-site copy failed", "fail", ago=timedelta(hours=3))
        ops("error", "API error · 500 on POST /api/x/", "fail", ago=timedelta(hours=30), unresolved=False)
        ops("deploy", "Old promote", ago=timedelta(days=10))
        Event.objects.create(name="admin.customer_unblocked", actor_email="priya@zinwe.io")

    def whats(self, **params: str) -> list[str]:
        response = self.client.get(LOGS, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        return [r["what"] for r in response.json()["rows"]]

    def test_range(self) -> None:
        self.assertEqual(len(self.whats(range="24h")), 3)
        self.assertEqual(len(self.whats(range="7d")), 4)
        self.assertIn("Old promote", self.whats(range="30d"))

    def test_type(self) -> None:
        self.assertEqual(self.whats(type="deploy"), ["Shipped to dev · backend"])
        self.assertEqual(self.whats(type="staff"), ["Unblocked customer a customer"])
        # "Errors" also lists every failed job, as on the board.
        self.assertEqual(self.whats(type="error"), ["Nightly backup · off-site copy failed", "API error · 500 on POST /api/x/"])

    def test_counts_follow_range_and_search_not_type(self) -> None:
        body = self.client.get(LOGS, {"type": "deploy", "q": "backup"}).json()
        self.assertEqual(body["counts"], {"all": 2, "server": 0, "deploy": 1, "backup": 1, "agent": 0, "staff": 0, "error": 1})
        self.assertEqual(body["total"], 1)

    def test_search_matches_who_what_and_details(self) -> None:
        self.assertEqual(self.whats(q="priya@"), ["Unblocked customer a customer"])
        self.assertEqual(self.whats(q="F3A9D21"), ["Shipped to dev · backend"])
        self.assertEqual(self.whats(q="nothing like this"), [])

    def test_limit_caps_and_flags_truncation(self) -> None:
        body = self.client.get(LOGS, {"limit": "2"}).json()
        self.assertEqual((len(body["rows"]), body["total"], body["truncated"]), (2, 4, True))

    def test_bad_filters_are_400(self) -> None:
        for params in ({"range": "1y"}, {"type": "mail"}, {"limit": "lots"}):
            self.assertEqual(self.client.get(LOGS, params).status_code, status.HTTP_400_BAD_REQUEST, params)


class LogsStatusTests(AdminApiTestCase):
    def test_status_card(self) -> None:
        ops("deploy", "Shipped to dev", ago=timedelta(hours=2))
        ops("backup", "Nightly backup", ago=timedelta(hours=26))
        failed = ops("backup", "Nightly backup · off-site copy failed", "fail", ago=timedelta(hours=1))
        body = self.client.get(STATUS).json()
        self.assertEqual(body["env"], "local")
        self.assertTrue(body["api"]["ok"])
        self.assertTrue(body["database"]["ok"])
        self.assertIsNone(body["uptime_30d"])  # not measured: the UI hides it
        self.assertEqual(body["last_deploy"]["what"], "Shipped to dev")
        self.assertEqual(body["last_backup"]["ops_id"], failed.pk)
        self.assertEqual(body["unresolved"], 1)
        self.assertEqual([a["ops_id"] for a in body["attention"]], [failed.pk])
        for key in ("cpu", "memory", "disk", "booted_at"):
            self.assertIn(key, body["server"])  # numbers on Linux, null where the OS doesn't say
        self.assertEqual(self.client.get("/api/admin/badges/").json()["logs_attention"], 1)

    def test_resolve(self) -> None:
        failed = ops("backup", "Nightly backup · off-site copy failed", "fail")
        url = f"{LOGS}ops/{failed.pk}/resolve/"
        row = self.client.post(url).json()
        self.assertFalse(row["unresolved"])
        self.assertTrue(any(d[0] == "Resolved" and "ops@zinwe.local" in d[1] for d in row["details"]))
        self.client.post(url)  # twice: one audit row
        self.assertEqual(Event.objects.filter(name="admin.log_resolved", actor_email="ops@zinwe.local").count(), 1)
        self.assertEqual(self.client.get(STATUS).json()["unresolved"], 0)
        self.assertEqual(self.client.post(f"{LOGS}ops/999999/resolve/").status_code, status.HTTP_404_NOT_FOUND)


@override_settings(ZINWE_ENV="")
class OpsEventCommandTests(AdminApiTestCase):
    def run_cmd(self, *args: str) -> OpsEvent:
        out = StringIO()
        call_command("ops_event", *args, stdout=out)
        return OpsEvent.objects.get(pk=int(out.getvalue().strip()))

    def test_writes_a_row_with_details_and_steps(self) -> None:
        event = self.run_cmd(
            "--type", "deploy", "--label", "Ship", "--what", "Shipped to dev · 20261030-091500", "--env", "dev",
            "--who", "vishal@zinwe.io", "--detail", "Backend=f3a9d21 Log backup size", "--detail", "Note=a=b",
            "--step", "Tests|ok|925 passed", "--step", "Health check|skip", "--source", "zinwe-server",
        )
        self.assertEqual((event.type, event.label, event.env, event.who, event.result), ("deploy", "Ship", "dev", "vishal@zinwe.io", "ok"))
        self.assertEqual(event.details, [["Backend", "f3a9d21 Log backup size"], ["Note", "a=b"]])
        self.assertEqual(event.steps, [["Tests", "ok", "925 passed"], ["Health check", "skip", ""]])
        self.assertFalse(event.unresolved)
        self.assertIn("Shipped to dev · 20261030-091500", [r["what"] for r in self.client.get(LOGS).json()["rows"]])

    def test_env_defaults_to_this_environment(self) -> None:
        self.assertEqual(self.run_cmd("--type", "backup", "--what", "Backup").env, "local")
        with override_settings(ZINWE_ENV="prod"):
            self.assertEqual(self.run_cmd("--type", "backup", "--what", "Backup").env, "prod")

    def test_a_failure_stays_open_until_an_ok_row_of_its_kind(self) -> None:
        failed = self.run_cmd("--type", "backup", "--result", "fail", "--what", "Off-site copy failed", "--env", "prod")
        self.assertTrue(failed.unresolved)
        self.run_cmd("--type", "deploy", "--what", "Shipped", "--env", "prod")  # another kind: no
        self.run_cmd("--type", "backup", "--what", "Backup OK", "--env", "dev")  # another env: no
        self.run_cmd("--type", "backup", "--what", "Backup OK", "--env", "prod", "--keep-open")
        failed.refresh_from_db()
        self.assertTrue(failed.unresolved)
        self.run_cmd("--type", "backup", "--what", "Backup OK", "--env", "prod")
        failed.refresh_from_db()
        self.assertFalse(failed.unresolved)
        self.assertIsNotNone(failed.resolved_at)

    def test_rollback_marks_the_release_it_undid(self) -> None:
        older = ops("deploy", "Promoted to prod · A", env="prod", label="Promote", ago=timedelta(days=1))
        newest = ops("deploy", "Promoted to prod · B", env="prod", label="Promote", ago=timedelta(hours=1))
        ops("deploy", "Shipped to dev · C", env="dev", label="Ship", ago=timedelta(minutes=30))
        self.run_cmd("--type", "deploy", "--label", "Rollback", "--what", "Rolled back prod to A", "--env", "prod",
                     "--marks-rolled")
        newest.refresh_from_db()
        older.refresh_from_db()
        self.assertEqual((newest.result, older.result), ("rolled", "ok"))
        self.assertEqual(newest.details[-1][0], "Rolled back")
        self.assertEqual(OpsEvent.objects.get(what="Shipped to dev · C").result, "ok")

    def test_resolved_flag(self) -> None:
        self.assertFalse(self.run_cmd("--type", "server", "--result", "fail", "--what", "Down 4 min", "--resolved").unresolved)

    def test_bad_input_is_refused(self) -> None:
        for args in (["--type", "deploy", "--what", " "], ["--type", "deploy", "--what", "x", "--detail", "novalue"],
                     ["--type", "deploy", "--what", "x", "--step", "Tests|maybe"]):
            with self.assertRaises(CommandError, msg=args):
                call_command("ops_event", *args, stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command("ops_event", "--type", "mail", "--what", "x", stdout=StringIO(), stderr=StringIO())
        self.assertFalse(OpsEvent.objects.exists())


class EnvironmentTests(AdminApiTestCase):
    @override_settings(ZINWE_ENV="")
    def test_flavour_and_release_from_the_release_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = Path(tmp) / "srv" / "zinwe" / "prod" / "releases" / "20261030-091500" / "backend"
            backend.mkdir(parents=True)
            (backend.parent / "RELEASE").write_text("flavour=prod\nbackend=f3a9d21 Log backup size\ndeployed_at=2026-10-30T09:20:00Z\n")
            with mock.patch("z_analytics.ops._base_dir", return_value=Path("/srv/zinwe/prod/releases/20261030-091500/backend")):
                self.assertEqual(environment(), "prod")
            with mock.patch("z_analytics.ops._base_dir", return_value=backend):
                info = release_info()
        self.assertEqual(info["backend"], "f3a9d21 Log backup size")
        self.assertEqual(info["deployed_at"], "2026-10-30T09:20:00Z")
        self.assertEqual(environment(), "local")
        self.assertIsNone(release_info())


def _boom(request: HttpRequest, pk: int):  # type: ignore[no-untyped-def]
    raise RuntimeError("secret detail that must not be logged")


urlpatterns = [path("fr37-boom/<int:pk>/", _boom)]


@override_settings(ROOT_URLCONF="z_adminapi.test_logs")
class ErrorRowTests(AdminApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        logger = logging.getLogger("django.request")
        self.handler = next(h for h in logger.handlers if isinstance(h, OpsErrorHandler))
        # Only the ops handler for these tests (no tracebacks on the console); assertLogs would replace it too.
        saved = logger.handlers[:]
        logger.handlers = [self.handler]
        self.addCleanup(setattr, logger, "handlers", saved)
        self.handler._last.clear()
        self.handler._repeats.clear()
        self.handler._hour.clear()
        self.client.raise_request_exception = False

    def test_a_500_becomes_one_rate_limited_error_row(self) -> None:
        self.assertEqual(self.client.get("/fr37-boom/1/").status_code, 500)
        self.client.get("/fr37-boom/2/")  # same route + exception within the window: counted, not written
        rows = list(OpsEvent.objects.filter(type="error"))
        self.assertEqual(len(rows), 1)
        event = rows[0]
        self.assertEqual(event.what, "API error · 500 on GET /fr37-boom/<int:pk>/ · RuntimeError")
        self.assertEqual((event.result, event.unresolved, event.source), ("fail", False, "django.request"))
        self.assertNotIn("secret detail", str(event.details))
        self.assertEqual(self.handler._repeats, {("GET", "/fr37-boom/<int:pk>/", "RuntimeError"): 1})

    def test_hourly_cap(self) -> None:
        with mock.patch.object(OpsErrorHandler, "HOURLY_CAP", 0):
            self.client.get("/fr37-boom/1/")
        self.assertFalse(OpsEvent.objects.exists())

    def test_never_raises(self) -> None:
        with mock.patch("z_analytics.ops.record", side_effect=RuntimeError("db down")), \
                self.assertLogs("zinwe.analytics", level="WARNING"):
            self.assertEqual(self.client.get("/fr37-boom/1/").status_code, 500)
        self.assertFalse(OpsEvent.objects.exists())
        # A non-500 record and junk records are ignored quietly.
        self.handler.emit(logging.makeLogRecord({"status_code": 404, "levelno": logging.ERROR}))
        self.handler.emit(logging.makeLogRecord({"status_code": "x"}))
        self.assertFalse(OpsEvent.objects.exists())
