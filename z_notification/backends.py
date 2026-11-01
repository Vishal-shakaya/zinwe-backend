"""
Email delivery with a fallback provider.

`FailoverEmailBackend` sends every message through the primary SMTP server (Brevo, as
hello@zinwe.io). If the primary refuses or fails -- daily limit reached, login or IP blocked,
network trouble -- the same message is sent through the fallback SMTP server (the team Gmail)
instead, and the primary is skipped for `EMAIL_FAILOVER_PAUSE_SECONDS` so later emails don't each
wait on a failing server first.

Gmail only sends as its own address, so a fallback email goes out From the fallback address with
Reply-To pointing at the original sender: replies still reach hello@zinwe.io.

Settings (all from env, see config/settings.py):
    EMAIL_HOST / EMAIL_PORT / EMAIL_HOST_USER / EMAIL_HOST_PASSWORD / EMAIL_USE_TLS   primary
    EMAIL_FALLBACK_HOST / _PORT / _HOST_USER / _HOST_PASSWORD / _USE_TLS / _FROM       fallback
    EMAIL_FAILOVER_PAUSE_SECONDS   how long the primary is skipped after it fails (default 600)
    EMAIL_TIMEOUT                  seconds per SMTP connection, so a dead server can't hang a request
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Sequence

from django.conf import settings
from django.core.mail import EmailMessage
from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail.backends.smtp import EmailBackend as SMTPBackend

logger = logging.getLogger("zinwe.notification.email")

# Process-wide: when the primary failed last. A worker restart forgets it, which is fine.
_paused_until = 0.0
_pause_lock = threading.Lock()


def primary_paused() -> bool:
    return time.monotonic() < _paused_until


def pause_primary(seconds: float) -> None:
    global _paused_until
    with _pause_lock:
        _paused_until = time.monotonic() + seconds


def reset_pause() -> None:
    """For tests."""
    global _paused_until
    with _pause_lock:
        _paused_until = 0.0


def _smtp(prefix: str) -> SMTPBackend | None:
    """An SMTP backend from EMAIL_<prefix>* settings, or None when no host is configured."""
    host = getattr(settings, f"EMAIL_{prefix}HOST", "")
    if not host:
        return None
    return SMTPBackend(
        host=host,
        port=getattr(settings, f"EMAIL_{prefix}PORT", 587),
        username=getattr(settings, f"EMAIL_{prefix}HOST_USER", ""),
        password=getattr(settings, f"EMAIL_{prefix}HOST_PASSWORD", ""),
        use_tls=getattr(settings, f"EMAIL_{prefix}USE_TLS", True),
        timeout=getattr(settings, "EMAIL_TIMEOUT", None) or 20,
        fail_silently=False,
    )


def outreach_connection() -> SMTPBackend:
    """The outreach mailbox (EMAIL_OUTREACH_*). Raises when it isn't set up: outreach never borrows the product account."""
    connection = _smtp("OUTREACH_")
    if connection is None:
        raise RuntimeError("The outreach mailbox is not set up (EMAIL_OUTREACH_HOST)")
    return connection


class FailoverEmailBackend(BaseEmailBackend):
    """Primary SMTP first; on any failure, the fallback SMTP. See the module docstring."""

    def __init__(self, fail_silently: bool = False, **kwargs: Any) -> None:
        super().__init__(fail_silently=fail_silently)
        self.primary = _smtp("")
        self.fallback = _smtp("FALLBACK_")

    def send_messages(self, email_messages: Sequence[EmailMessage]) -> int:
        sent = 0
        for message in email_messages:
            try:
                if self._send_one(message):
                    sent += 1
            except Exception:
                if not self.fail_silently:
                    raise
        return sent

    def _send_one(self, message: EmailMessage) -> bool:
        if getattr(message, "primary_only", False):
            # Bulk mail (the newsletter) goes through the primary or not at all: the backup is a personal Gmail
            # account. Tried even while the primary is paused; a failure is raised so the caller can retry later.
            if self.primary is None:
                raise RuntimeError("No primary SMTP configured for a primary-only message")
            if self.primary.send_messages([message]):
                return True
            raise RuntimeError("primary SMTP accepted no message")
        primary_error: Exception | None = None
        if self.primary is not None and not primary_paused():
            try:
                if self.primary.send_messages([message]):
                    return True
                primary_error = RuntimeError("primary SMTP accepted no message")
            except Exception as exc:  # noqa: BLE001 -- quota, auth, IP block, network: all mean "try the fallback"
                primary_error = exc
            pause = float(getattr(settings, "EMAIL_FAILOVER_PAUSE_SECONDS", 600))
            pause_primary(pause)
            logger.warning(
                "Primary SMTP failed (%s: %s); using the fallback and skipping the primary for %ss",
                type(primary_error).__name__, primary_error, int(pause),
            )

        if self.fallback is None:
            if primary_error is not None:
                raise primary_error
            raise RuntimeError("No SMTP server available: the primary is paused and no fallback is configured")

        return self._send_fallback(message)

    def _send_fallback(self, message: EmailMessage) -> bool:
        fallback_from = getattr(settings, "EMAIL_FALLBACK_FROM", "") or message.from_email
        original_from, original_reply_to = message.from_email, list(message.reply_to)
        if fallback_from != original_from:
            message.from_email = fallback_from
            if not message.reply_to:
                message.reply_to = [original_from]  # replies still reach hello@zinwe.io
        try:
            ok = bool(self.fallback.send_messages([message]))
        finally:
            message.from_email, message.reply_to = original_from, original_reply_to
        if ok:
            logger.info("Sent through the fallback SMTP: %r to %s", message.subject, ", ".join(message.to))
        return ok
