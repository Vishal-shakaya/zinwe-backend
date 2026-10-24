"""
Taking back claim rows a crash left behind (FIX-009).

The newsletter, the series drip and guide follow-ups each claim a row ("sending") before an email goes out and
record the result after. A process that dies in between (worker killed, deploy restart, out of memory) leaves the
row in "sending" for good: the newsletter issue never reaches "sent", and a drip episode or follow-up counts as done
so the member never gets it.

Each hourly job first calls `reclaim()` for its rows. A claim older than `STALE_AFTER` (wall clock; a send takes
seconds) is settled once, by a conditional update so concurrent runs can't both act on it:
- the email went out (a "sent" Notification to that address for that article / guide since the claim): the row is
  recorded as sent, nothing is resent;
- otherwise it is released (`release_to`): the newsletter puts it back to "pending" (the same run sends it), the drip
  and follow-ups mark it "failed" (their usual retry: the member's next send day).
The claim-row design is unchanged, so nobody gets an email twice through these jobs.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable

from django.db.models import Model, Q, QuerySet
from django.utils import timezone

from z_main.refs import resolve_ref
from z_notification.models import Notification

logger = logging.getLogger("zinwe.blog.claims")

STALE_AFTER = timedelta(minutes=45)
INTERRUPTED = "Interrupted mid-send (the process stopped); sending again."
SLACK = timedelta(minutes=1)  # the Notification row is written a moment before the send


def cutoff() -> datetime:
    return timezone.now() - STALE_AFTER


def delivered(email: str, ref: Model, since: datetime | None) -> Notification | None:
    """The "sent" Notification of this email (to `email`, about `ref`) written since the claim, if any."""
    ref_type, ref_id = resolve_ref(ref)
    found = Notification.objects.filter(
        channel="email", recipient=email, ref_type=ref_type, ref_id=ref_id, status=Notification.Status.SENT
    )
    if since is not None:
        found = found.filter(created_at__gte=since - SLACK)
    return found.order_by("created_at").first()


def reclaim(
    stale: QuerySet[Any],
    *,
    ref: Callable[[Any], Model],
    since: Callable[[Any], datetime | None],
    release_to: str,
    sent: str = "sent",
    sending: str = "sending",
) -> tuple[int, int]:
    """Settle every stale claim in `stale` (rows in `sending` claimed before `cutoff()`). Returns (recorded as sent,
    released for another send)."""
    recorded = released = 0
    for row in stale.select_related("subscriber"):
        claimed_at = getattr(row, "claimed_at", None)
        mine = type(row).objects.filter(pk=row.pk, status=sending)
        mine = mine.filter(claimed_at=claimed_at) if claimed_at is not None else mine.filter(claimed_at__isnull=True)
        note = delivered(row.subscriber.email, ref(row), since(row))
        if note is not None:
            if mine.update(status=sent, sent_at=note.sent_at or note.created_at, error=""):
                recorded += 1
        elif mine.update(status=release_to, error=INTERRUPTED[:300]):
            released += 1
    if recorded or released:
        logger.warning("Took back %s stale claims: %s already sent, %s to send again", recorded + released, recorded, released)
    return recorded, released


def stale_q(legacy_field: str) -> Q:
    """Claimed before the cutoff; rows from before `claimed_at` existed are judged by `legacy_field`."""
    edge = cutoff()
    return Q(claimed_at__lt=edge) | Q(claimed_at__isnull=True, **{f"{legacy_field}__lt": edge})
