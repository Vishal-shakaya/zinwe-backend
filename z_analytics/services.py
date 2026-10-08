"""Event tracking and summary metrics."""
from __future__ import annotations

import logging
from collections import Counter
from datetime import timedelta
from typing import Any

from django.db.models import Count
from django.utils import timezone

from z_main.refs import Ref, resolve_ref

from .models import Event

logger = logging.getLogger("zinwe.analytics")


def track(
    name: str,
    properties: dict[str, Any] | None = None,
    actor_email: str = "",
    ref: Ref = None,
) -> Event | None:
    """Record one event. Never raises; returns None if recording failed."""
    try:
        ref_type, ref_id = resolve_ref(ref)
        return Event.objects.create(
            name=name,
            properties=properties or {},
            actor_email=(actor_email or "").strip().lower(),
            ref_type=ref_type,
            ref_id=ref_id,
        )
    except Exception:  # noqa: BLE001 -- analytics must never break the caller
        logger.exception("Could not track event %r", name)
        return None


def build_summary() -> dict[str, Any]:
    """Aggregate early-access and event counts for the admin summary endpoint."""
    from z_auth.models import EarlyAccessRegistration as EAR

    registrations = EAR.objects.all()
    since = timezone.now() - timedelta(days=7)

    by_team_size: dict[str, int] = {value: 0 for value, _ in EAR.TeamSize.choices}
    for row in registrations.values("team_size").annotate(n=Count("id")):
        by_team_size[row["team_size"]] = row["n"]

    concern_counts: Counter[str] = Counter()
    for concerns in registrations.values_list("concerns", flat=True):
        concern_counts.update(concerns or [])
    by_concern = {value: concern_counts.get(value, 0) for value in EAR.CONCERN_CHOICES}

    return {
        "early_access": {
            "total": registrations.count(),
            "last_7_days": registrations.filter(created_at__gte=since).count(),
            "by_team_size": by_team_size,
            "by_concern": by_concern,
        },
        "events": {"total": Event.objects.count()},
    }
