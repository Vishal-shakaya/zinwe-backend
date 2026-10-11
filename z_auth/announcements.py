"""
Dashboard notification-bar items (M11): the version/offer/upgrade messages shown
under the nav.

- "version" and "offer" banners are set per environment via
  ``settings.ZINWE_ANNOUNCEMENTS`` (the Zinwe team toggles them without a deploy).
- The "upgrade" nudge is derived live from the owner's current subscription
  against the active plan catalogue, so it never hard-codes plan codes.

Each item is a plain dict the frontend renders and can remember-dismiss by ``id``.
"""
from __future__ import annotations

from typing import Any

from django.conf import settings

from .billing import current_subscription
from .models import Plan, Subscription, User

ALLOWED_KINDS = {"version", "offer", "upgrade"}


def _config_announcements() -> list[dict[str, Any]]:
    """Validate the configured version/offer banners, dropping malformed entries."""
    out: list[dict[str, Any]] = []
    for raw in settings.ZINWE_ANNOUNCEMENTS or []:
        if not isinstance(raw, dict):
            continue
        ident = str(raw.get("id", "")).strip()
        kind = str(raw.get("kind", "offer")).strip()
        title = str(raw.get("title", "")).strip()
        # The upgrade nudge is derived, never configured; only version/offer here.
        if not ident or not title or kind not in {"version", "offer"}:
            continue
        out.append(
            {
                "id": ident,
                "kind": kind,
                "title": title,
                "body": str(raw.get("body", "")).strip(),
                "cta_label": str(raw.get("cta_label", "")).strip(),
                "cta_href": str(raw.get("cta_href", "")).strip(),
                "dismissible": bool(raw.get("dismissible", True)),
            }
        )
    return out


def _upgrade_announcement(user: User) -> dict[str, Any] | None:
    """Suggest the next paid plan up, if the owner isn't already on the top one."""
    subscription = current_subscription(user)
    # Don't nudge while a payment is pending/under review — that's its own banner.
    if subscription is not None and subscription.status != Subscription.Status.ACTIVE:
        return None

    current = subscription.plan if subscription else None
    current_sort = current.sort_order if current else -1
    candidates = (
        Plan.objects.filter(is_active=True, sort_order__gt=current_sort)
        .exclude(price_inr_monthly__lte=0)  # a real upsell means a paid plan
        .order_by("sort_order", "code")
    )
    # Only suggest a plan that actually covers more PCs than the current one
    # (0 = "not set", treated as no known cap, so it never blocks a suggestion).
    current_devices = current.max_devices if current else 0
    target = next(
        (p for p in candidates if not current_devices or not p.max_devices or p.max_devices > current_devices),
        None,
    )
    if target is None:
        return None

    current_name = current.name if current else "the free plan"
    body = f"You're on {current_name}"
    if current and current.max_devices:
        body += f" ({current.max_devices} PCs)."
    else:
        body += "."
    if target.max_devices:
        body += f" {target.name} covers {target.max_devices} PCs."
    return {
        "id": f"upgrade:{current.code if current else 'none'}:{target.code}",
        "kind": "upgrade",
        "title": f"Upgrade to {target.name}",
        "body": body,
        "cta_label": "See plans",
        "cta_href": "/app/settings/billing",
        "dismissible": True,
    }


def announcements_for(user: User) -> list[dict[str, Any]]:
    """The notification-bar items for this owner: configured banners, then the upgrade nudge."""
    items = _config_announcements()
    upgrade = _upgrade_announcement(user)
    if upgrade is not None:
        items.append(upgrade)
    return items
