"""
Email one-time codes (6 digits) for sign-up, sign-in and password reset.

Rules (portal-v1 §2):
- valid for 10 minutes; 5 wrong attempts burn the code;
- 60 s cooldown between sends, at most 5 sends per email + purpose per hour;
- a new code invalidates older unused codes for the same email + purpose;
- only an HMAC-SHA256 (keyed with SECRET_KEY) is stored, compared in constant time.

This module only issues and checks codes. Sending the email is the caller's job
(see `services.send_code_email`), so callers can decide not to send to an
address without an account while still counting the request (no enumeration).
"""
from __future__ import annotations

import hashlib
import hmac
import math
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import OneTimeCode

Purpose = OneTimeCode.Purpose

CODE_LENGTH = 6
CODE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5
RESEND_COOLDOWN_SECONDS = 60
MAX_SENDS_PER_HOUR = 5
# Rows older than this are deleted opportunistically when a new code is issued.
RETENTION = timedelta(days=1)


class OtpThrottled(Exception):
    """A code can't be sent yet. `retry_after` is in whole seconds; str(exc) is user-facing."""

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))


def normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def hash_code(email: str, purpose: str, code: str) -> str:
    """HMAC-SHA256 of the code, bound to the email and purpose."""
    message = f"{purpose}:{normalise_email(email)}:{code}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), message, hashlib.sha256).hexdigest()


def generate_code() -> str:
    return f"{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}"


def _seconds_until(moment) -> int:  # type: ignore[no-untyped-def]
    return math.ceil((moment - timezone.now()).total_seconds())


def check_can_send(email: str, purpose: str) -> None:
    """Raise `OtpThrottled` if the cooldown or the hourly cap blocks a new code."""
    email = normalise_email(email)
    now = timezone.now()
    recent = OneTimeCode.objects.filter(
        email=email, purpose=purpose, created_at__gt=now - timedelta(hours=1)
    ).order_by("-created_at")
    latest = recent.first()
    if latest is not None:
        ready_at = latest.created_at + timedelta(seconds=RESEND_COOLDOWN_SECONDS)
        if ready_at > now:
            wait = _seconds_until(ready_at)
            raise OtpThrottled(
                f"Please wait {wait} seconds before asking for another code.", wait
            )
    count = recent.count()
    if count >= MAX_SENDS_PER_HOUR:
        oldest = recent[MAX_SENDS_PER_HOUR - 1]
        wait = _seconds_until(oldest.created_at + timedelta(hours=1))
        minutes = max(1, math.ceil(wait / 60))
        raise OtpThrottled(
            f"Too many codes requested for this email. Try again in {minutes} minute"
            f"{'' if minutes == 1 else 's'}.",
            wait,
        )


@transaction.atomic
def issue_code(email: str, purpose: str, ip: str | None = None) -> str:
    """
    Create a new code for email + purpose and return it (plain, for the email).

    Raises `OtpThrottled` (cooldown / hourly cap). Older unused codes for the
    same email + purpose stop working.
    """
    email = normalise_email(email)
    check_can_send(email, purpose)
    now = timezone.now()
    OneTimeCode.objects.filter(created_at__lt=now - RETENTION).delete()
    OneTimeCode.objects.filter(
        email=email, purpose=purpose, consumed_at__isnull=True, expires_at__gt=now
    ).update(expires_at=now)
    code = generate_code()
    OneTimeCode.objects.create(
        email=email,
        purpose=purpose,
        code_hash=hash_code(email, purpose, code),
        expires_at=now + CODE_TTL,
        created_at=now,
        ip=ip,
    )
    return code


def verify_code(email: str, purpose: str, code: str) -> bool:
    """
    Check a code. True once per code (it is consumed on success).

    A wrong guess counts against the newest live code; after `MAX_ATTEMPTS`
    wrong guesses that code is burnt. Call it *outside* any surrounding
    transaction, so a wrong attempt is recorded even if later work rolls back.
    """
    email = normalise_email(email)
    code = (code or "").strip()
    with transaction.atomic():
        now = timezone.now()
        otp = (
            OneTimeCode.objects.select_for_update()
            .filter(
                email=email,
                purpose=purpose,
                consumed_at__isnull=True,
                expires_at__gt=now,
                attempts__lt=MAX_ATTEMPTS,
            )
            .order_by("-created_at", "-id")
            .first()
        )
        if otp is None:
            return False
        if hmac.compare_digest(otp.code_hash, hash_code(email, purpose, code)):
            otp.consumed_at = now
            otp.save(update_fields=["consumed_at"])
            return True
        OneTimeCode.objects.filter(pk=otp.pk).update(attempts=F("attempts") + 1)
        return False
