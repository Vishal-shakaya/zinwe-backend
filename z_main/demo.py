"""
Demo accounts: a made-up company used for product videos and screenshots
(`manage.py seed_demo_company`). Its addresses use ZINWE_DEMO_EMAIL_DOMAINS, so:

- no email to them is ever delivered (z_notification.services.notify logs it as sent with a
  "demo address" note, so screens still say "Emailed"), and
- the admin dashboard's headline numbers leave them out (z_adminapi.services.dashboard_metrics).

The domains are fictional and did not exist when chosen; never put a real customer's domain here.
"""
from __future__ import annotations

from django.conf import settings
from django.db.models import Q


def demo_domains() -> tuple[str, ...]:
    return tuple(d.strip().lower().lstrip("@") for d in settings.ZINWE_DEMO_EMAIL_DOMAINS if d.strip())


def is_demo_email(email: str | None) -> bool:
    domain = (email or "").rpartition("@")[2].strip().lower()
    return bool(domain) and domain in demo_domains()


def demo_email_q(field: str = "email") -> Q:
    """Matches rows whose `field` is a demo address (an empty Q-that-matches-nothing when none are set)."""
    q = Q(pk__in=[])
    for domain in demo_domains():
        q |= Q(**{f"{field}__iendswith": f"@{domain}"})
    return q
