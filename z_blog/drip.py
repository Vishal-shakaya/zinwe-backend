"""
The series drip: every confirmed subscriber gets a series' released episodes by email, one by one from
Episode 1, one a day on the series' send days (default Mon, Tue, Thu, Fri: four a week), from
`drip_hour` in the subscriber's own time zone. Episode 1 goes out the moment they confirm.

A subscriber who joins late starts at Episode 1 and catches up at the same pace; one who catches up with
the releases simply waits for the next episode to go live. `run_jobs` calls `send_due()` every hour.

Idempotent like the newsletter: each (subscriber, episode) is one `SeriesSend` row, claimed before its
email goes out, so concurrent runs never send anything twice. A failed email is retried on the
subscriber's next send day. Demo-domain addresses are logged, never delivered (z_notification).
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.utils import timezone

from z_notification.models import Notification

from . import series as svc
from .models import Article, NewsletterSubscriber, Series, SeriesSend

logger = logging.getLogger("zinwe.blog.drip")

Sub = NewsletterSubscriber.Status
Send = SeriesSend.Status

DEFAULT_TZ = "Asia/Kolkata"
WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or getattr(settings, "ZINWE_SERIES_DEFAULT_TZ", DEFAULT_TZ))
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def valid_zone(name: str) -> str:
    """The IANA zone name if it's a real one, else "" (the default zone is used)."""
    name = (name or "").strip()[:64]
    if not name:
        return ""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ""
    return name


def weekdays(series: Series) -> set[int]:
    days = {int(d) for d in (series.drip_weekdays or "").split(",") if d.strip().isdigit() and int(d) <= 6}
    return days or {0, 1, 3, 4}


def clean_weekdays(raw: object) -> str:
    """"0,1,3,4" from a list of ints or a comma string; refuses an empty set."""
    items = raw if isinstance(raw, (list, tuple)) else str(raw or "").split(",")
    days = sorted({int(d) for d in items if str(d).strip().isdigit() and 0 <= int(d) <= 6})
    if not days:
        raise ValueError("Pick at least one send day.")
    return ",".join(str(d) for d in days)


def subject_for(article: Article) -> str:
    return (article.newsletter_subject or article.title)[:200]


@dataclass
class DripResult:
    sent: int = 0
    failed: int = 0
    waiting: int = 0  # subscribers who have every released episode already

    def summary(self) -> str:
        return f"{self.sent} sent, {self.failed} failed, {self.waiting} up to date"


def _claim(series: Series, sub: NewsletterSubscriber, article: Article, today: date) -> SeriesSend | None:
    """Take this (subscriber, episode) for sending, or None if it's sent or another sender has it."""
    try:
        with transaction.atomic():
            return SeriesSend.objects.create(series=series, subscriber=sub, article=article, local_date=today)
    except IntegrityError:
        pass
    # A failed earlier try: take it again (only one sender wins the update).
    taken = SeriesSend.objects.filter(subscriber=sub, article=article, status=Send.FAILED).update(
        status=Send.SENDING, local_date=today, error=""
    )
    return SeriesSend.objects.get(subscriber=sub, article=article) if taken else None


def _deliver(row: SeriesSend, sub: NewsletterSubscriber, article: Article) -> bool:
    from .newsletter import _send  # the same email and headers as every newsletter (one-click unsubscribe)

    note = _send(article, subject_for(article), sub, sub.email)
    if note is not None and note.status == Notification.Status.SENT:
        row.status, row.sent_at = Send.SENT, timezone.now()
        row.save(update_fields=["status", "sent_at"])
        return True
    row.status = Send.FAILED
    row.error = (note.error if note is not None else "Not sent")[:300]
    row.save(update_fields=["status", "error"])
    return False


def _next_episode(released: list[Article], done: set[int]) -> Article | None:
    return next((a for a in released if a.pk not in done), None)


def send_due(now: datetime | None = None, limit: int = 500) -> DripResult:
    """Send today's episode to every subscriber whose send time has come (all drip series)."""
    now = now or timezone.now()
    result = DripResult()
    budget = limit
    shows = Series.objects.filter(status=Series.Status.PUBLISHED, drip_enabled=True)
    for show in shows:
        released = svc.lineup(show, now).released
        if not released:
            continue
        days = weekdays(show)
        subs = list(NewsletterSubscriber.objects.filter(status=Sub.ACTIVE, confirmed_at__isnull=False))
        rows = SeriesSend.objects.filter(series=show, subscriber__in=subs).values_list(
            "subscriber_id", "article_id", "status", "local_date"
        )
        done: dict[int, set[int]] = {}
        last_day: dict[int, date] = {}
        for sub_id, article_id, status, day in rows:
            # A failed try still counts as that day's email (retried on the next send day, not every hour),
            # but the episode stays due.
            if status != Send.FAILED:
                done.setdefault(sub_id, set()).add(article_id)
            if sub_id not in last_day or day > last_day[sub_id]:
                last_day[sub_id] = day
        for sub in subs:
            local = now.astimezone(zone(sub.timezone))
            if local.weekday() not in days or local.hour < show.drip_hour:
                continue
            if last_day.get(sub.pk) == local.date():
                continue  # today's episode already went (or was tried)
            article = _next_episode(released, done.get(sub.pk, set()))
            if article is None:
                result.waiting += 1
                continue
            row = _claim(show, sub, article, local.date())
            if row is None:
                continue
            if _deliver(row, sub, article):
                result.sent += 1
            else:
                result.failed += 1
            budget -= 1
            if budget <= 0:
                return result
    return result


def welcome_series(sub: NewsletterSubscriber) -> Series | None:
    """The one drip series whose Episode 1 goes out on confirming: the series they signed up on (their `source`
    is /blog/series/<slug>...), else the first by position. The others start on their next send day, so a new
    member never gets two welcome emails at once."""
    shows = list(Series.objects.filter(status=Series.Status.PUBLISHED, drip_enabled=True))
    for show in shows:
        if (sub.source or "").startswith(f"/blog/series/{show.slug}"):
            return show
    return shows[0] if shows else None


def welcome(sub: NewsletterSubscriber, now: datetime | None = None) -> int:
    """Just confirmed: Episode 1 of their welcome series (welcome_series), straight away (whatever the day).
    Returns emails sent."""
    now = now or timezone.now()
    sent = 0
    first = welcome_series(sub)
    for show in [first] if first is not None else []:
        released = svc.lineup(show, now).released
        if not released:
            continue
        done = set(SeriesSend.objects.filter(subscriber=sub, series=show).exclude(status=Send.FAILED)
                   .values_list("article_id", flat=True))
        if done:
            continue  # a returning subscriber carries on from where they were, on the normal schedule
        row = _claim(show, sub, released[0], now.astimezone(zone(sub.timezone)).date())
        if row is not None and _deliver(row, sub, released[0]):
            sent += 1
    return sent


def _welcome_in_background(sub_id: int) -> None:
    try:
        close_old_connections()
        sub = NewsletterSubscriber.objects.filter(pk=sub_id, status=Sub.ACTIVE).first()
        if sub is not None:
            welcome(sub)
    except Exception:  # noqa: BLE001 -- the hourly run sends it instead
        logger.exception("Series welcome for subscriber %s failed", sub_id)
    finally:
        connection.close()


def start_welcome(sub: NewsletterSubscriber) -> None:
    """Send the welcome episode without holding up the confirm request; `send_due` is the safety net."""
    if getattr(settings, "ZINWE_SERIES_WELCOME_INLINE", False):  # tests: run it in the request
        welcome(sub)
        return
    pk = sub.pk
    transaction.on_commit(
        lambda: threading.Thread(target=_welcome_in_background, args=(pk,), name=f"series-welcome-{pk}", daemon=True).start()
    )


def stats(show: Series) -> dict[str, int]:
    """For the admin: how far the drip has gone."""
    rows = SeriesSend.objects.filter(series=show)
    today = timezone.localdate()
    return {
        "readers": rows.filter(status=Send.SENT).values("subscriber").distinct().count(),
        "sent": rows.filter(status=Send.SENT).count(),
        "failed": rows.filter(status=Send.FAILED).count(),
        "sent_today": rows.filter(status=Send.SENT, sent_at__date=today).count(),
    }


def episode_sent(article: Article) -> int:
    return SeriesSend.objects.filter(article=article, status=Send.SENT).count()


def schedule_label(show: Series) -> str:
    days = sorted(weekdays(show))
    return f"{', '.join(WEEKDAY_NAMES[d] for d in days)} from {show.drip_hour:02d}:00 (reader's time)"


# --- Per-member unlocking -----------------------------------------------------------
# With the drip on, a member's episodes open as their emails arrive: an episode is theirs once the drip has
# taken it for them (sent, or tried). Episode 1 is always theirs (it goes out the moment they confirm).
# With the drip off, every released episode is open to every member.


def unlocked_ids(sub: NewsletterSubscriber, show: Series) -> set[int]:
    return set(SeriesSend.objects.filter(subscriber=sub, series=show).values_list("article_id", flat=True))


def member_unlocked(article: Article, sub: NewsletterSubscriber) -> bool:
    show = article.series
    if not show.drip_enabled:
        return True
    released = svc.lineup(show).released
    if released and released[0].pk == article.pk:
        return True
    return article.pk in unlocked_ids(sub, show)


def _send_days_from(show: Series, sub: NewsletterSubscriber, now: datetime, include_today: bool):
    """Yield the subscriber's coming send dates (their local calendar)."""
    days = weekdays(show)
    day = now.astimezone(zone(sub.timezone)).date()
    if not include_today:
        day += timedelta(days=1)
    for _ in range(400):
        if day.weekday() in days:
            yield day
        day += timedelta(days=1)


def _sent_today(show: Series, sub: NewsletterSubscriber, now: datetime) -> bool:
    today = now.astimezone(zone(sub.timezone)).date()
    return SeriesSend.objects.filter(series=show, subscriber=sub, local_date=today).exists()


def unlocks_on(article: Article, sub: NewsletterSubscriber, now: datetime | None = None) -> date | None:
    """The day this episode's email should reach this member (None if it's theirs already or the drip is off)."""
    now = now or timezone.now()
    show = article.series
    if not show.drip_enabled or member_unlocked(article, sub):
        return None
    done = unlocked_ids(sub, show)
    queue = [a.pk for a in svc.lineup(show, now).released if a.pk not in done]
    if article.pk not in queue:
        return None
    k = queue.index(article.pk)
    for i, day in enumerate(_send_days_from(show, sub, now, include_today=not _sent_today(show, sub, now))):
        if i == k:
            return day
    return None


def next_for(sub: NewsletterSubscriber, current: Article, now: datetime | None = None) -> tuple[Article | None, date | None]:
    """For an episode email: the member's next episode and the day it arrives (after today)."""
    now = now or timezone.now()
    show = current.series
    done = unlocked_ids(sub, show) | {current.pk}
    nxt = next((a for a in svc.lineup(show, now).released if a.pk not in done), None)
    if nxt is None or not show.drip_enabled:
        return nxt, None
    return nxt, next(_send_days_from(show, sub, now, include_today=False), None)


def progress(sub: NewsletterSubscriber, show: Series) -> dict[str, int]:
    released = svc.lineup(show).released
    if not show.drip_enabled:
        return {"unlocked": len(released), "released": len(released)}
    ids = unlocked_ids(sub, show) | ({released[0].pk} if released else set())
    return {"unlocked": sum(1 for a in released if a.pk in ids), "released": len(released)}
