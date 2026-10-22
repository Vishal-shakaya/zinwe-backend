"""
FR-19: small, kind tags for each member, from their last 7 days — a bit of fun for the owner ("Early bird",
"On a streak", "Deep focus"). Only ever positive or neutral: nothing here may shame anyone, and nothing is shown to
the member. At most three per person, in the order below. Each says why, in plain words.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any

from django.db.models import Count, Sum
from django.utils import timezone

from z_auth import entitlements as plan
from z_auth.entitlements import entitlements_for
from z_auth.models import User

from .models import Member, Message, UsageMetric, WorkLog

WINDOW_DAYS = 7
MAX_TAGS = 3


def _tag(key: str, emoji: str, label: str, why: str) -> dict[str, str]:
    return {"key": key, "emoji": emoji, "label": label, "why": why}


def tags_for_owner(owner: User, now: Any = None) -> dict[str, list[dict[str, str]]]:
    """{member id (as text): [tag, ...]} for the owner's active members."""
    now = now or timezone.now()
    since = now - timedelta(days=WINDOW_DAYS)
    ent = entitlements_for(owner)
    members = list(Member.objects.filter(owner=owner, is_active=True))
    if not members:
        return {}
    ids = [m.pk for m in members]

    # Work days: first log-in, last log-out, break minutes, per member per local day.
    first_in: dict[int, dict[Any, Any]] = defaultdict(dict)
    last_out: dict[int, dict[Any, Any]] = defaultdict(dict)
    break_secs: dict[int, dict[Any, int]] = defaultdict(lambda: defaultdict(int))
    if ent.has(plan.WORK_TIME):
        logs = WorkLog.objects.filter(owner=owner, member_id__in=ids, at__gte=since).order_by("member_id", "at")
        previous: dict[int, WorkLog] = {}
        for log in logs:
            local = timezone.localtime(log.at)
            day = local.date()
            mid = log.member_id
            prev = previous.get(mid)
            if prev is not None and prev.state == WorkLog.State.ON_BREAK and timezone.localtime(prev.at).date() == day:
                break_secs[mid][day] += int((log.at - prev.at).total_seconds())
            if log.state == WorkLog.State.WORKING and day not in first_in[mid]:
                first_in[mid][day] = local
            if log.state == WorkLog.State.ENDED:
                last_out[mid][day] = local
            previous[mid] = log

    # App focus and website variety (only where the plan collects them).
    app_secs: dict[int, list[int]] = defaultdict(list)
    if ent.has(plan.APP_USAGE):
        rows = (
            UsageMetric.objects.filter(device__member_id__in=ids, category="app.foreground", bucket_start__gte=since)
            .exclude(key="")
            .values("device__member_id", "key")
            .annotate(s=Sum("seconds"))
        )
        for r in rows:
            app_secs[r["device__member_id"]].append(r["s"] or 0)
    sites: dict[int, int] = {}
    if ent.has(plan.WEB):
        for r in (
            UsageMetric.objects.filter(device__member_id__in=ids, category="web.domain", bucket_start__gte=since)
            .exclude(key="")
            .values("device__member_id")
            .annotate(n=Count("key", distinct=True))
        ):
            sites[r["device__member_id"]] = r["n"]
    replies = dict(
        Message.objects.filter(owner=owner, member_id__in=ids, sender=Message.Sender.MEMBER, created_at__gte=since)
        .values_list("member_id")
        .annotate(n=Count("id"))
    )

    out: dict[str, list[dict[str, str]]] = {}
    for m in members:
        tags: list[dict[str, str]] = []
        days_in = first_in[m.pk]
        early = sum(1 for t in days_in.values() if (t.hour, t.minute) < (9, 30))
        late = sum(1 for t in last_out[m.pk].values() if t.hour >= 20)
        if len(days_in) >= 5:
            tags.append(_tag("streak", "🔥", "On a streak", f"Worked {len(days_in)} of the last 7 days."))
        if early >= 3:
            tags.append(_tag("early_bird", "🌅", "Early bird", f"Started before 9:30 AM on {early} days this week."))
        total_app = sum(app_secs[m.pk])
        if total_app >= 2 * 3600 and max(app_secs[m.pk], default=0) >= 0.6 * total_app:
            tags.append(_tag("deep_focus", "🎯", "Deep focus", "Spent most of their PC time in one app this week."))
        breaks = [s for s in break_secs[m.pk].values() if s > 0]
        if len(breaks) >= 3 and 20 * 60 <= sum(breaks) / len(breaks) <= 75 * 60:
            tags.append(_tag("balanced", "☕", "Break balancer", "Took steady, regular breaks this week."))
        if replies.get(m.pk, 0) >= 5:
            tags.append(_tag("team_talker", "💬", "Team talker", f"Replied {replies[m.pk]} times in Chat this week."))
        if late >= 2:
            tags.append(_tag("night_owl", "🦉", "Night owl", f"Logged out after 8 PM on {late} days this week."))
        if sites.get(m.pk, 0) >= 20:
            tags.append(_tag("explorer", "🧭", "Explorer", f"Visited {sites[m.pk]} different websites this week."))
        if now - m.created_at <= timedelta(days=WINDOW_DAYS):
            tags.append(_tag("new_here", "🌱", "New here", "Joined the team this week."))
        out[str(m.pk)] = tags[:MAX_TAGS]
    return out
