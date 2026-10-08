"""Request metadata helpers (client IP, user agent)."""
from __future__ import annotations

import ipaddress

from django.conf import settings
from django.http import HttpRequest


def _valid_ip(value: str) -> str | None:
    value = (value or "").strip()
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return None
    return value


def get_client_ip(request: HttpRequest) -> str | None:
    """
    Best-effort client IP.

    Uses the first hop of X-Forwarded-For only when ZINWE_TRUST_X_FORWARDED_FOR
    is on (i.e. behind a trusted proxy); otherwise REMOTE_ADDR. Returns None
    if no valid IP is available.
    """
    if getattr(settings, "ZINWE_TRUST_X_FORWARDED_FOR", False):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            first_hop = _valid_ip(forwarded.split(",")[0])
            if first_hop:
                return first_hop
    return _valid_ip(request.META.get("REMOTE_ADDR", ""))


def get_user_agent(request: HttpRequest, max_length: int = 255) -> str:
    """The request's User-Agent, truncated to fit the DB column."""
    return (request.META.get("HTTP_USER_AGENT", "") or "")[:max_length]
