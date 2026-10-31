"""
Web push (FR-15): the owner hears about a chat reply, an alert, a request or a task the moment it happens, with the
dashboard closed and on Android. public/sw.js in zinwe-frontend shows what arrives.

The browser gives us a subscription (an endpoint at its vendor's push service, plus keys); we post to that endpoint and
the vendor wakes the browser. The text is encrypted for that browser (RFC 8291): the vendor sees the size and the time,
not the words. `owner_feed.items_for()` decides what an owner gets and what it says, so it's the same as the dashboard's
30-second poll, and the feed item id is the notification's tag on both paths (the browser shows one, not two).

Off until WEBPUSH_VAPID_PUBLIC_KEY, _PRIVATE_KEY and _SUBJECT are all set: the API says so, nothing is sent, and the
open dashboard's poll works as before.

Sending never touches the request that caused it: `announce()` waits for the commit, then a background thread posts
(as z_blog.drip does). There is no retry and `run_jobs` isn't involved: a late "wants to take a break" is noise, and
an open dashboard's poll shows anything a lost push missed. 404 / 410 from the push service means that browser is gone,
so its row is deleted; any other failure is logged and dropped. The endpoint is a secret address and the text is
private: only the host and status go in the log.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Iterable
from urllib.parse import urlsplit

from django.conf import settings
from django.db import close_old_connections, connection, transaction
from django.utils import timezone

from z_auth.models import User

from . import owner_feed
from .models import PushSubscription

logger = logging.getLogger("zinwe.devices.push")

MAX_PER_OWNER = 10  # browsers; the oldest goes first
MAX_AT_ONCE = 5  # notifications from one event batch, then one summary (the same as the dashboard's poll)
MAX_THREADS = 8  # sending at once in this process; above this a push is dropped, the request never waits
TIMEOUT_SECONDS = 5
TTL_SECONDS = 4 * 60 * 60  # the push service holds it this long for a sleeping phone; later it's stale news
GONE = {404, 410}

# Who may be posted to: the browsers' push services, so a saved address can't aim this server at anything else.
# Chrome, Samsung, Opera, Brave: Google. Firefox: Mozilla. Edge: Microsoft. Safari: Apple.
PUSH_HOSTS = ("googleapis.com", "google.com", "mozilla.com", "notify.windows.com", "push.apple.com")

_slots = threading.BoundedSemaphore(MAX_THREADS)


def enabled() -> bool:
    return bool(
        settings.WEBPUSH_VAPID_PUBLIC_KEY and settings.WEBPUSH_VAPID_PRIVATE_KEY and settings.WEBPUSH_VAPID_SUBJECT
    )


def status() -> dict[str, Any]:
    """What the dashboard needs before it subscribes: public_key is the browser's applicationServerKey."""
    on = enabled()
    return {"enabled": on, "public_key": settings.WEBPUSH_VAPID_PUBLIC_KEY if on else ""}


def is_push_service(endpoint: str) -> bool:
    parts = urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.port not in (None, 443):
        return False
    return any(host == known or host.endswith(f".{known}") for known in PUSH_HOSTS)


def save_subscription(owner: User, endpoint: str, p256dh: str, auth: str, user_agent: str = "") -> PushSubscription:
    """
    One row per browser, so subscribing again is safe. An endpoint belongs to a browser, not an account: when someone
    else signs in on that browser, the row moves to them (they, not the last person, should get what it shows).
    """
    row, _ = PushSubscription.objects.update_or_create(
        endpoint=endpoint, defaults={"owner": owner, "p256dh": p256dh, "auth": auth, "user_agent": user_agent[:255]}
    )
    extra = PushSubscription.objects.filter(owner=owner).order_by("-created_at", "-id").values_list("pk", flat=True)
    PushSubscription.objects.filter(pk__in=list(extra[MAX_PER_OWNER:])).delete()
    return row


def remove_subscription(owner: User, endpoint: str) -> None:
    """This owner's browser only; an address they don't hold (or that's already gone) is simply nothing to do."""
    PushSubscription.objects.filter(owner=owner, endpoint=endpoint).delete()


def announce(owner_id: int, kind: str, ids: Iterable[int | None]) -> None:
    """
    Something new for the owner: `kind` is a key of `owner_feed.KINDS`, `ids` its rows. Call it from the service that
    saved them. After the commit a thread sends it; nothing here can slow or fail the caller.
    """
    wanted = [pk for pk in ids if pk is not None]
    if wanted and enabled():
        transaction.on_commit(lambda: _start(owner_id, kind, wanted), robust=True)


def _start(owner_id: int, kind: str, ids: list[int]) -> None:
    # Most owners have no browser subscribed: no thread and no extra connection for each upload from their PCs.
    if not PushSubscription.objects.filter(owner_id=owner_id).exists():
        return
    if getattr(settings, "ZINWE_PUSH_INLINE", False):  # tests: run it in the request
        send(owner_id, kind, ids)
        return
    if not _slots.acquire(blocking=False):
        logger.warning("Push dropped: %s sends are already running", MAX_THREADS)
        return
    try:
        threading.Thread(
            target=_send_in_background, args=(owner_id, kind, ids), name=f"push-{kind}-{owner_id}", daemon=True
        ).start()
    except BaseException:
        _slots.release()
        raise


def _send_in_background(owner_id: int, kind: str, ids: list[int]) -> None:
    try:
        close_old_connections()
        send(owner_id, kind, ids)
    finally:
        _slots.release()
        connection.close()


def send(owner_id: int, kind: str, ids: Iterable[int]) -> int:
    """Post what these rows mean to each of the owner's browsers. Returns how many took it. Never raises."""
    try:
        if not enabled():
            return 0
        owner = User.objects.filter(pk=owner_id, is_active=True).first()
        subscriptions = list(PushSubscription.objects.filter(owner_id=owner_id)) if owner else []
        if not subscriptions:
            return 0
        payloads = _payloads(owner_feed.items_for(owner, kind, ids))
        if not payloads:
            return 0
        return sum(_deliver(subscription, payloads) for subscription in subscriptions)
    except Exception:  # noqa: BLE001 -- a push is a courtesy: never the cause of a failure
        logger.exception("Push for owner %s failed", owner_id)
        return 0


def _payloads(items: list[dict[str, Any]]) -> list[str]:
    """What the service worker needs, short and small: push services take about 4 KB."""
    fields = [{k: item[k] for k in ("id", "title", "body", "link")} for item in items[:MAX_AT_ONCE]]
    extra = len(items) - MAX_AT_ONCE
    if extra > 0:
        fields.append({
            "id": "zinwe-more", "title": f"{extra} more updates in Zinwe", "body": "Open Zinwe to see them all.",
            "link": "/app",
        })
    return [json.dumps(f, ensure_ascii=False, separators=(",", ":")) for f in fields]


def _deliver(subscription: PushSubscription, payloads: list[str]) -> int:
    """1 when this browser took them all. On the first failure it stops: the rest would fail the same way."""
    for payload in payloads:
        try:
            _post(subscription, payload)
        except Exception as exc:  # noqa: BLE001 -- logged and dropped, by design
            code = getattr(getattr(exc, "response", None), "status_code", None)
            if code in GONE:
                PushSubscription.objects.filter(pk=subscription.pk).delete()
            else:
                host = urlsplit(subscription.endpoint).hostname
                logger.warning("Push to %s failed: %s", host, code or type(exc).__name__)
            return 0
    PushSubscription.objects.filter(pk=subscription.pk).update(last_success_at=timezone.now())
    return 1


def _post(subscription: PushSubscription, payload: str) -> None:
    from pywebpush import webpush

    webpush(
        subscription_info={"endpoint": subscription.endpoint, "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth}},
        data=payload,
        vapid_private_key=settings.WEBPUSH_VAPID_PRIVATE_KEY,
        vapid_claims={"sub": settings.WEBPUSH_VAPID_SUBJECT},  # a new dict each time: webpush adds this endpoint's "aud"
        ttl=TTL_SECONDS,
        timeout=TIMEOUT_SECONDS,
        headers={"Urgency": "high"},  # wakes a phone in Doze; every push shows a notification, so it's the honest level
    )
