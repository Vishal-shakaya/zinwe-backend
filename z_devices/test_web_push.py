"""
FR-15 web push: the owner's browser hears about a reply, alert, request or task with the dashboard closed.

pywebpush is replaced by a fake module (`FakeWebPush`), so these tests run the real `_post` with or without the library
installed, and nothing leaves the machine. Live events go through the real endpoints the agent calls.
"""
from __future__ import annotations

import json
import sys
import types
from datetime import timedelta
from typing import Any
from unittest import mock

from django.test import override_settings
from django.utils import timezone
from rest_framework import status

from z_auth.models import User

from . import web_push
from .models import Device, DeviceEvent, Member, Message, NotificationPrefs, PushSubscription, Task, TaskActivity, WorkLog
from .owner_feed import feed
from .test_plan_limits import PlanTestCase
from .test_tasks import TaskTestCase
from .tests import HEARTBEAT_URL, MESSAGES_URL, UPLOAD_URL, make_event

STATUS_URL = "/api/devices/notifications/push/"
SUBSCRIBE_URL = "/api/devices/notifications/push/subscribe/"
UNSUBSCRIBE_URL = "/api/devices/notifications/push/unsubscribe/"

ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"
OTHER_ENDPOINT = "https://updates.push.services.mozilla.com/wpush/v2/def456"
KEYS = {"p256dh": "B" + "x" * 86, "auth": "y" * 22}

KEYS_ON = {
    "WEBPUSH_VAPID_PUBLIC_KEY": "test-public-key",
    "WEBPUSH_VAPID_PRIVATE_KEY": "test-private-key",
    "WEBPUSH_VAPID_SUBJECT": "mailto:test@example.com",
}
KEYS_OFF = {"WEBPUSH_VAPID_PUBLIC_KEY": "", "WEBPUSH_VAPID_PRIVATE_KEY": "", "WEBPUSH_VAPID_SUBJECT": ""}


class FakeWebPush:
    """Stands in for the pywebpush module. `fail` is raised by every send."""

    class WebPushException(Exception):
        def __init__(self, message: str, response: Any = None) -> None:
            super().__init__(message)
            self.response = response

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail: Exception | None = None

    def webpush(self, **kwargs: Any) -> None:
        kwargs["payload"] = json.loads(kwargs["data"])
        self.calls.append(kwargs)
        if self.fail:
            raise self.fail

    def rejected(self, code: int) -> Exception:
        return self.WebPushException("Push failed", response=types.SimpleNamespace(status_code=code, text="secret"))

    @property
    def tags(self) -> list[str]:
        return [c["payload"]["id"] for c in self.calls]


class PushMixin:
    """Keys on, sends in the request, and a fake pywebpush that records what would have gone out."""

    def start_push(self) -> None:
        overrides = override_settings(**KEYS_ON, ZINWE_PUSH_INLINE=True)
        overrides.enable()
        self.addCleanup(overrides.disable)  # type: ignore[attr-defined]
        self.push = FakeWebPush()
        modules = mock.patch.dict(sys.modules, {"pywebpush": self.push})
        modules.start()
        self.addCleanup(modules.stop)  # type: ignore[attr-defined]

    def browser(self, owner: User | None = None, endpoint: str = ENDPOINT) -> PushSubscription:
        return PushSubscription.objects.create(
            owner=owner or self.owner, endpoint=endpoint, p256dh=KEYS["p256dh"], auth=KEYS["auth"],  # type: ignore[attr-defined]
        )

    def prefs(self, **fields: Any) -> None:
        NotificationPrefs.objects.update_or_create(owner=self.owner, defaults=fields)  # type: ignore[attr-defined]


class ApiTests(PushMixin, TaskTestCase):
    """The three owner endpoints. `self.token` is the enrolled PC's."""

    def setUp(self) -> None:
        super().setUp()
        self.start_push()
        self.as_owner()

    def subscribe(self, endpoint: str = ENDPOINT, keys: dict[str, str] = KEYS):  # type: ignore[no-untyped-def]
        return self.client.post(SUBSCRIBE_URL, {"endpoint": endpoint, "keys": keys}, format="json")

    def test_only_a_signed_in_owner_gets_in(self) -> None:
        calls = [("get", STATUS_URL), ("post", SUBSCRIBE_URL), ("post", UNSUBSCRIBE_URL)]
        self.client.credentials()
        for method, url in calls:
            self.assertEqual(getattr(self.client, method)(url).status_code, status.HTTP_401_UNAUTHORIZED, url)
        self.as_device(self.token)
        for method, url in calls:
            code = getattr(self.client, method)(url).status_code
            self.assertIn(code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN), url)

    def test_status_gives_the_public_key_and_never_the_private_one(self) -> None:
        response = self.client.get(STATUS_URL)
        self.assertEqual(response.json(), {"enabled": True, "public_key": "test-public-key"})
        self.assertNotIn("test-private-key", response.content.decode())

    def test_subscribe_saves_this_browser(self) -> None:
        response = self.client.post(
            SUBSCRIBE_URL, {"endpoint": ENDPOINT, "keys": KEYS}, format="json", HTTP_USER_AGENT="Chrome on Android",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertEqual(response.json(), {"subscribed": True})
        row = PushSubscription.objects.get()
        self.assertEqual(
            (row.owner, row.endpoint, row.p256dh, row.auth, row.user_agent),
            (self.owner, ENDPOINT, KEYS["p256dh"], KEYS["auth"], "Chrome on Android"),
        )
        self.assertIsNotNone(row.created_at)
        self.assertIsNone(row.last_success_at)

    def test_subscribing_twice_keeps_one_row_with_the_new_keys(self) -> None:
        self.subscribe()
        new_keys = {"p256dh": "B" + "z" * 86, "auth": "w" * 22}
        self.assertEqual(self.subscribe(keys=new_keys).status_code, status.HTTP_200_OK)
        row = PushSubscription.objects.get()
        self.assertEqual((row.p256dh, row.auth), (new_keys["p256dh"], new_keys["auth"]))

    def test_someone_else_signing_in_on_that_browser_takes_it_over(self) -> None:
        other = User.objects.create_user("other@acme.in", "pw-12345678")
        self.browser(owner=other)
        self.assertEqual(self.subscribe().status_code, status.HTTP_200_OK)
        self.assertEqual(PushSubscription.objects.get().owner, self.owner)

    def test_only_the_browsers_push_services_are_accepted(self) -> None:
        for good in (
            ENDPOINT,
            OTHER_ENDPOINT,
            "https://wns2-pn1p.notify.windows.com/w/?token=abc",
            "https://web.push.apple.com/QAbc",
        ):
            self.assertEqual(self.subscribe(good).status_code, status.HTTP_200_OK, good)
        for bad in (
            "http://fcm.googleapis.com/fcm/send/abc",
            "https://localhost/fcm/send/abc",
            "https://169.254.169.254/latest/meta-data",
            "https://evil.example/fcm.googleapis.com",
            "https://fcm.googleapis.com.evil.example/x",
            "https://notgoogleapis.com/x",
            "https://fcm.googleapis.com:8443/x",
            "not a url",
        ):
            self.assertEqual(self.subscribe(bad).status_code, status.HTTP_400_BAD_REQUEST, bad)
        self.assertEqual(PushSubscription.objects.count(), 4)

    def test_keys_must_look_like_keys(self) -> None:
        for keys in ({"p256dh": "short", "auth": KEYS["auth"]}, {"p256dh": KEYS["p256dh"], "auth": "a b"}, {}):
            self.assertEqual(self.subscribe(keys=keys).status_code, status.HTTP_400_BAD_REQUEST, keys)
        self.assertFalse(PushSubscription.objects.exists())

    def test_a_browser_over_the_limit_replaces_the_oldest(self) -> None:
        for i in range(web_push.MAX_PER_OWNER + 2):
            self.assertEqual(self.subscribe(f"{ENDPOINT}{i}").status_code, status.HTTP_200_OK)
        rows = set(PushSubscription.objects.values_list("endpoint", flat=True))
        self.assertEqual(len(rows), web_push.MAX_PER_OWNER)
        self.assertNotIn(f"{ENDPOINT}0", rows)
        self.assertIn(f"{ENDPOINT}{web_push.MAX_PER_OWNER + 1}", rows)

    def test_unsubscribe_removes_this_browser_and_is_safe_to_repeat(self) -> None:
        self.browser()
        self.browser(endpoint=OTHER_ENDPOINT)
        for _ in range(2):
            response = self.client.post(UNSUBSCRIBE_URL, {"endpoint": ENDPOINT}, format="json")
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.json(), {"subscribed": False})
        self.assertEqual(list(PushSubscription.objects.values_list("endpoint", flat=True)), [OTHER_ENDPOINT])

    def test_unsubscribe_cant_touch_another_owners_browser(self) -> None:
        other = User.objects.create_user("other@acme.in", "pw-12345678")
        self.browser(owner=other)
        self.client.post(UNSUBSCRIBE_URL, {"endpoint": ENDPOINT}, format="json")
        self.assertEqual(PushSubscription.objects.get().owner, other)

    def test_keys_unset_the_api_says_so_and_nothing_is_saved(self) -> None:
        for missing in KEYS_ON:  # any one missing is off
            with override_settings(**{missing: ""}):
                self.assertEqual(self.client.get(STATUS_URL).json(), {"enabled": False, "public_key": ""})
                response = self.subscribe()
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(response.json(), {"subscribed": False})
        self.assertFalse(PushSubscription.objects.exists())

    def test_unsubscribe_still_works_with_the_keys_unset(self) -> None:
        self.browser()
        with override_settings(**KEYS_OFF):
            self.assertEqual(self.client.post(UNSUBSCRIBE_URL, {"endpoint": ENDPOINT}, format="json").status_code, 200)
        self.assertFalse(PushSubscription.objects.exists())


class LiveEventTests(PushMixin, TaskTestCase):
    """Each kind, from the endpoint that causes it. `self.member` has a PC; `self.token` is its."""

    def setUp(self) -> None:
        super().setUp()
        self.start_push()
        self.device = Device.objects.get()
        self.browser()

    def reply(self, body: str = "Done, sending the file now"):  # type: ignore[no-untyped-def]
        self.as_device(self.token)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(MESSAGES_URL, {"body": body}, format="json")

    def upload(self, *events: dict[str, Any]):  # type: ignore[no-untyped-def]
        self.as_device(self.token)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(UPLOAD_URL, {"events": list(events)}, format="json")

    def take_a_break(self, reason: str = "Lunch", **beat: Any):  # type: ignore[no-untyped-def]
        """What the Windows app sends: the change list, as a current agent; `beat` overrides for an older one."""
        at = timezone.now() - timedelta(seconds=30)
        event = {"at": at.isoformat(), "state": "on_break", "reason": reason, "note": ""}
        self.as_device(self.token)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(HEARTBEAT_URL, {"work_state": "on_break", "work_events": [event], **beat}, format="json")

    def member_task(self, action: str, **extra: Any) -> int:
        task = self.make()["id"]
        self.push.calls.clear()
        response = self.member_act(task, action, **extra)  # runs the on-commit hooks itself
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        return task

    def test_a_reply_arrives_as_the_same_item_the_feed_gives(self) -> None:
        since = timezone.now() - timedelta(seconds=1)
        self.assertEqual(self.reply().status_code, status.HTTP_201_CREATED)

        self.assertEqual(len(self.push.calls), 1)
        call = self.push.calls[0]
        item = feed(self.owner, since)["items"][0]
        # The feed item id is the tag on both paths (the browser shows one notification, not two).
        self.assertEqual(call["payload"], {k: item[k] for k in ("id", "title", "body", "link")})
        self.assertEqual(call["payload"]["id"], f"chat-{Message.objects.get(sender='member').pk}")
        self.assertEqual(call["subscription_info"], {"endpoint": ENDPOINT, "keys": KEYS})
        self.assertEqual(call["vapid_private_key"], "test-private-key")
        self.assertEqual(call["vapid_claims"], {"sub": "mailto:test@example.com"})
        self.assertGreater(call["ttl"], 0)  # 0 = dropped unless the phone is awake right now
        self.assertTrue(call["timeout"])
        self.assertIsNotNone(PushSubscription.objects.get().last_success_at)

    def test_nothing_goes_out_before_the_commit(self) -> None:
        self.as_device(self.token)
        with self.captureOnCommitCallbacks(execute=False) as waiting:
            self.client.post(MESSAGES_URL, {"body": "hello"}, format="json")
        self.assertEqual(self.push.calls, [])
        self.assertTrue(waiting)

    def test_every_browser_gets_it_with_its_own_audience(self) -> None:
        self.browser(endpoint=OTHER_ENDPOINT)
        self.reply()
        self.assertEqual(
            sorted(c["subscription_info"]["endpoint"] for c in self.push.calls), sorted([ENDPOINT, OTHER_ENDPOINT])
        )
        # webpush writes "aud" (one push service) into the claims it's given: they must not be shared.
        self.assertIsNot(self.push.calls[0]["vapid_claims"], self.push.calls[1]["vapid_claims"])

    def test_another_owners_browser_gets_nothing(self) -> None:
        other = User.objects.create_user("other@acme.in", "pw-12345678")
        PushSubscription.objects.all().delete()
        self.browser(owner=other)
        self.reply()
        self.assertEqual(self.push.calls, [])

    def test_a_suspended_account_gets_nothing(self) -> None:
        User.objects.filter(pk=self.owner.pk).update(is_active=False)
        self.reply()
        self.assertEqual(self.push.calls, [])

    def test_an_alert_at_the_owners_level_arrives(self) -> None:
        self.upload(make_event(type="usb.blocked", severity="warning"), make_event(severity="info"))
        event = DeviceEvent.objects.get(severity="warning")
        self.assertEqual(self.push.tags, [f"alert-{event.pk}"])
        self.assertEqual(self.push.calls[0]["payload"]["link"], "/app/alerts")

    def test_a_resent_upload_doesnt_push_again(self) -> None:
        event = make_event(type="usb.blocked", severity="warning")
        self.upload(event)
        self.upload(event)
        self.assertEqual(len(self.push.calls), 1)

    def test_a_rule_confirmation_is_not_an_alert(self) -> None:
        self.upload(make_event(type="usb.restored", severity="critical"))
        self.assertEqual(self.push.calls, [])

    def test_a_big_batch_is_five_and_a_summary(self) -> None:
        self.upload(*[make_event(type="usb.blocked", severity="warning") for _ in range(8)])
        self.assertEqual(len(self.push.calls), web_push.MAX_AT_ONCE + 1)
        last = self.push.calls[-1]["payload"]
        self.assertEqual((last["id"], last["title"]), ("zinwe-more", "3 more updates in Zinwe"))

    def test_a_break_request_from_the_current_agent(self) -> None:
        self.assertEqual(self.take_a_break().status_code, status.HTTP_200_OK)
        log = WorkLog.objects.get(state="on_break")
        self.assertEqual(log.approval, WorkLog.Approval.PENDING)
        self.assertEqual(self.push.tags, [f"approval-{log.pk}"])
        payload = self.push.calls[0]["payload"]
        self.assertEqual((payload["title"], payload["body"]), ("Asha Rao wants to take a break", "Lunch"))
        self.assertEqual(payload["link"], f"/app/activity?member={self.member.id}")

    def test_a_break_request_from_an_older_agent(self) -> None:
        self.as_device(self.token)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(HEARTBEAT_URL, {"work_state": "on_break", "break_reason": "Tea"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.push.tags, [f"approval-{WorkLog.objects.get(state='on_break').pk}"])

    def test_a_log_out_asks_too_and_starting_work_doesnt(self) -> None:
        now = timezone.now()
        events = [
            {"at": (now - timedelta(seconds=60)).isoformat(), "state": "working", "reason": "", "note": ""},
            {"at": (now - timedelta(seconds=30)).isoformat(), "state": "ended", "reason": "Doctor", "note": ""},
        ]
        self.as_device(self.token)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(HEARTBEAT_URL, {"work_state": "ended", "work_events": events}, format="json")
        self.assertEqual(self.push.tags, [f"approval-{WorkLog.objects.get(state='ended').pk}"])
        self.assertEqual(self.push.calls[0]["payload"]["title"], "Asha Rao wants to log out")

    def test_approvals_off_is_respected_on_both_paths(self) -> None:
        self.prefs(approvals=False)
        since = timezone.now() - timedelta(seconds=1)
        self.take_a_break()
        self.assertEqual(WorkLog.objects.get(state="on_break").approval, WorkLog.Approval.PENDING)  # still recorded
        self.assertEqual(self.push.calls, [])
        self.assertEqual(feed(self.owner, since)["items"], [])

    def test_task_help_and_done_arrive_but_other_moves_dont(self) -> None:
        task = self.member_task("help", note="Which ledger?")
        activity = TaskActivity.objects.get(task_id=task, action="help")
        self.assertEqual(self.push.tags, [f"task-{activity.pk}"])
        payload = self.push.calls[0]["payload"]
        self.assertEqual(payload["title"], "Asha Rao needs help with a task")
        self.assertEqual(payload["body"], "Reconcile October GST: Which ledger?")
        self.assertEqual(payload["link"], f"/app/tasks?task={task}")

        task = self.member_task("done")
        activity = TaskActivity.objects.get(task_id=task, action="done")
        self.assertEqual(self.push.tags, [f"task-{activity.pk}"])

        self.member_task("accept")
        self.assertEqual(self.push.calls, [])

    def test_prefs_off_silences_each_kind(self) -> None:
        self.prefs(chat=False, alerts="off", tasks=False, approvals=False)
        self.reply()
        self.upload(make_event(type="usb.blocked", severity="critical"))
        self.take_a_break()
        self.member_task("help", note="Stuck")
        self.assertEqual(self.push.calls, [])

    def test_alert_level_critical_drops_warnings(self) -> None:
        self.prefs(alerts="critical")
        self.upload(make_event(type="usb.blocked", severity="warning"))
        self.assertEqual(self.push.calls, [])
        self.upload(make_event(type="usb.blocked", severity="critical"))
        self.assertEqual(len(self.push.calls), 1)

    def test_a_failing_push_doesnt_fail_the_request(self) -> None:
        self.push.fail = RuntimeError("no route to host")
        with self.assertLogs("zinwe.devices.push", "WARNING") as logs:
            response = self.reply()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Message.objects.filter(sender="member").exists())
        self.assertTrue(PushSubscription.objects.exists())  # only 404 / 410 delete it
        self.assertIsNone(PushSubscription.objects.get().last_success_at)
        self.assertIn("fcm.googleapis.com failed: RuntimeError", logs.output[0])

    def test_pywebpush_missing_doesnt_fail_the_request(self) -> None:
        with mock.patch.dict(sys.modules, {"pywebpush": None}), self.assertLogs("zinwe.devices.push", "WARNING"):
            self.assertEqual(self.reply().status_code, status.HTTP_201_CREATED)
        self.assertTrue(PushSubscription.objects.exists())

    def test_an_error_building_the_push_doesnt_fail_the_request(self) -> None:
        with mock.patch("z_devices.owner_feed.items_for", side_effect=ValueError("boom")), self.assertLogs(
            "zinwe.devices.push", "ERROR"
        ):
            self.assertEqual(self.reply().status_code, status.HTTP_201_CREATED)

    def test_a_database_error_after_the_commit_doesnt_fail_the_request(self) -> None:
        with mock.patch.object(web_push, "PushSubscription", side_effect=None) as model, self.assertLogs("django.test", "ERROR"):
            model.objects.filter.side_effect = RuntimeError("database gone")
            self.assertEqual(self.reply().status_code, status.HTTP_201_CREATED)

    def test_keys_unset_nothing_is_sent_and_nothing_errors(self) -> None:
        with override_settings(**KEYS_OFF):
            self.assertEqual(self.reply().status_code, status.HTTP_201_CREATED)
            self.assertEqual(web_push.send(self.owner.pk, "chat", [Message.objects.get(sender="member").pk]), 0)
        self.assertEqual(self.push.calls, [])


class GoneTests(PushMixin, TaskTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.start_push()

    def reply(self) -> None:
        self.as_device(self.token)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(MESSAGES_URL, {"body": "hello"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_410_and_404_delete_the_subscription(self) -> None:
        for code in (410, 404):
            self.browser()
            self.push.fail = self.push.rejected(code)
            self.reply()
            self.assertFalse(PushSubscription.objects.exists(), code)

    def test_a_dead_browser_doesnt_stop_the_next_one(self) -> None:
        self.browser()
        alive = self.browser(endpoint=OTHER_ENDPOINT)

        def by_endpoint(**kwargs: Any) -> None:
            if kwargs["subscription_info"]["endpoint"] == ENDPOINT:
                raise self.push.rejected(410)

        with mock.patch.object(self.push, "webpush", side_effect=by_endpoint):
            self.reply()
        alive.refresh_from_db()
        self.assertEqual(list(PushSubscription.objects.all()), [alive])
        self.assertIsNotNone(alive.last_success_at)

    def test_other_rejections_keep_it_and_the_log_has_no_address_or_text(self) -> None:
        self.browser()
        for code in (413, 429, 500, 503):
            self.push.fail = self.push.rejected(code)
            with self.assertLogs("zinwe.devices.push", "WARNING") as logs:
                self.reply()
            self.assertIn(str(code), logs.output[0])
            self.assertNotIn("abc123", logs.output[0])
            self.assertNotIn("secret", logs.output[0])
        self.assertTrue(PushSubscription.objects.exists())

    def test_it_stops_at_the_first_failure_for_that_browser(self) -> None:
        self.browser()
        self.push.fail = self.push.rejected(500)
        with self.assertLogs("zinwe.devices.push", "WARNING"):
            sent = web_push._deliver(PushSubscription.objects.get(), ['{"id":"a"}', '{"id":"b"}'])
        self.assertEqual(sent, 0)
        self.assertEqual(len(self.push.calls), 1)


class PlanTests(PushMixin, PlanTestCase):
    """Free has USB alerts only; a push follows the plan exactly as the feed does."""

    def setUp(self) -> None:
        super().setUp()
        self.start_push()
        self.token = self.enroll().json()["device_token"]
        self.device = Device.objects.get()
        self.browser()

    def test_a_plan_without_chat_gets_no_chat_or_task_push(self) -> None:
        self.use_plan("startups")
        message = Message.objects.create(owner=self.owner, member=self.member, sender="member", body="hi")
        task = Task.objects.create(owner=self.owner, member=self.member, title="GST")
        moved = TaskActivity.objects.create(task=task, by="member", action="help", note="Stuck")
        self.assertEqual(web_push.send(self.owner.pk, "chat", [message.pk]), 0)
        self.assertEqual(web_push.send(self.owner.pk, "task", [moved.pk]), 0)
        self.assertEqual(self.push.calls, [])

    def test_a_plan_with_chat_gets_it(self) -> None:
        message = Message.objects.create(owner=self.owner, member=self.member, sender="member", body="hi")
        self.assertEqual(web_push.send(self.owner.pk, "chat", [message.pk]), 1)

    def test_a_locked_alert_kind_gets_no_push(self) -> None:
        self.use_plan(None)
        locked = DeviceEvent.objects.create(
            device=self.device, client_id=make_event()["id"], type="web.blocked", severity="critical",
            occurred_at=timezone.now(), data={},
        )
        self.assertEqual(web_push.send(self.owner.pk, "alert", [locked.pk]), 0)
        self.assertEqual(self.push.calls, [])

    def test_a_plan_without_work_hours_has_no_request_to_tell(self) -> None:
        self.use_plan(None)
        self.as_device(self.token)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(HEARTBEAT_URL, {"work_state": "on_break", "break_reason": "Tea"}, format="json")
        self.assertFalse(WorkLog.objects.exists())
        self.assertEqual(self.push.calls, [])


class BackgroundTests(PushMixin, TaskTestCase):
    """How it leaves the request: a thread, started after the commit, and never more than MAX_THREADS."""

    def setUp(self) -> None:
        super().setUp()
        self.start_push()
        self.browser()

    def reply(self):  # type: ignore[no-untyped-def]
        self.as_device(self.token)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(MESSAGES_URL, {"body": "hello"}, format="json")

    @override_settings(ZINWE_PUSH_INLINE=False)
    def test_it_is_sent_on_a_thread_of_its_own(self) -> None:
        with mock.patch.object(web_push.threading, "Thread") as thread:
            self.assertEqual(self.reply().status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.push.calls, [])
        kwargs = thread.call_args.kwargs
        self.assertIs(kwargs["target"], web_push._send_in_background)
        self.assertTrue(kwargs["daemon"])
        thread.return_value.start.assert_called_once()
        web_push._slots.release()  # the mocked thread never ran to give its slot back

    @override_settings(ZINWE_PUSH_INLINE=False)
    def test_no_thread_for_an_owner_with_no_browser(self) -> None:
        PushSubscription.objects.all().delete()
        with mock.patch.object(web_push.threading, "Thread") as thread:
            self.reply()
        thread.assert_not_called()

    @override_settings(ZINWE_PUSH_INLINE=False)
    def test_with_every_slot_taken_it_is_dropped_not_queued(self) -> None:
        taken = 0
        while web_push._slots.acquire(blocking=False):
            taken += 1
        try:
            with mock.patch.object(web_push.threading, "Thread") as thread, self.assertLogs("zinwe.devices.push", "WARNING"):
                self.assertEqual(self.reply().status_code, status.HTTP_201_CREATED)
            thread.assert_not_called()
        finally:
            for _ in range(taken):
                web_push._slots.release()

    @override_settings(ZINWE_PUSH_INLINE=False)
    def test_a_thread_that_cant_start_doesnt_fail_the_request_or_leak_a_slot(self) -> None:
        with mock.patch.object(web_push.threading, "Thread") as thread, self.assertLogs("django.test", "ERROR"):
            thread.return_value.start.side_effect = RuntimeError("can't start new thread")
            self.assertEqual(self.reply().status_code, status.HTTP_201_CREATED)
        acquired = 0
        while web_push._slots.acquire(blocking=False):
            acquired += 1
        for _ in range(acquired):
            web_push._slots.release()
        self.assertEqual(acquired, web_push.MAX_THREADS)

    def test_the_thread_gives_its_slot_back_and_closes_its_connection(self) -> None:
        web_push._slots.acquire()
        with (
            mock.patch.object(web_push, "send", side_effect=RuntimeError("boom")),
            mock.patch.object(web_push, "close_old_connections"),
            mock.patch.object(web_push, "connection") as connection,
        ):
            with self.assertRaises(RuntimeError):
                web_push._send_in_background(self.owner.pk, "chat", [1])
        connection.close.assert_called_once()
        web_push._slots.acquire(blocking=False)
        web_push._slots.release()


class PayloadTests(PushMixin, TaskTestCase):
    """The push is short: the feed's 140-character body, never the whole message."""

    def test_the_payload_stays_small(self) -> None:
        self.start_push()
        self.browser()
        Member.objects.filter(pk=self.member.pk).update(name="A" * 120)
        self.as_device(self.token)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(MESSAGES_URL, {"body": "long message " * 60}, format="json")
        call = self.push.calls[0]
        self.assertLessEqual(len(call["payload"]["body"]), 140)
        self.assertLess(len(call["data"].encode("utf-8")), 1024)
        self.assertEqual(set(call["payload"]), {"id", "title", "body", "link"})
