"""Where an owner is in the portal journey; the frontend routes on `step`."""
from __future__ import annotations

from typing import Any

from .billing import current_subscription, has_payment_under_review
from .company import get_company
from .models import Company, Subscription, User

Status = Subscription.Status

_UNSET: Any = object()


def onboarding_state(
    user: User,
    company: Company | None = _UNSET,
    subscription: Subscription | None = _UNSET,
) -> dict[str, Any]:
    """
    `{"step", "has_company", "subscription_status"}` per portal-v1 §3.1 (first match wins):
    company -> plan -> payment -> payment_review -> complete.

    Pass `company` / `subscription` when the caller already loaded them.
    """
    if company is _UNSET:
        company = get_company(user)
    if subscription is _UNSET:
        subscription = current_subscription(user)
    status = subscription.status if subscription is not None else None

    if company is None:
        step = "company"
    elif subscription is None or status in (Status.CANCELLED, Status.EXPIRED):
        step = "plan"
    elif status == Status.PENDING_PAYMENT:
        step = "payment_review" if has_payment_under_review(subscription) else "payment"
    else:
        step = "complete"
    return {"step": step, "has_company": company is not None, "subscription_status": status}
