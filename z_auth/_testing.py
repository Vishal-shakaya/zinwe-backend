"""Shared helpers for the z_auth portal tests (not a test module itself)."""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from django.core import mail
from django.core.cache import cache
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from .models import Company, OneTimeCode, User

STRONG_PASSWORD = "s3cure-Passphrase!"
CODE_RE = re.compile(r"^\s+(\d{6})\s*$", re.MULTILINE)

COMPANY = {
    "name": "Verma Textiles",
    "industry": "manufacturing",
    "team_size": "11-25",
    "phone": "+91 98100 00000",
    "city": "New Delhi",
    "state": "Delhi",
}


def codes_sent_to(email: str) -> list[str]:
    """Every code emailed to `email`, oldest first (read from the locmem outbox)."""
    found = []
    for message in mail.outbox:
        if email in message.to:
            match = CODE_RE.search(message.body)
            if match:
                found.append(match.group(1))
    return found


def last_code(email: str) -> str:
    codes = codes_sent_to(email)
    assert codes, f"no code was emailed to {email}"
    return codes[-1]


def age_codes(email: str, seconds: int = 61) -> None:
    """Pretend the codes for `email` were sent `seconds` earlier (skips the resend cooldown)."""
    for row in OneTimeCode.objects.filter(email=email):
        row.created_at -= timedelta(seconds=seconds)
        row.save(update_fields=["created_at"])


class PortalTestCase(APITestCase):
    """APITestCase with fresh throttle counters and on-commit side effects executed."""

    def setUp(self) -> None:
        cache.clear()  # reset DRF throttle counters between tests

    def post(self, url: str, data: Any = None, fmt: str | None = "json", **extra: Any):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(url, data, format=fmt, **extra)

    def patch(self, url: str, data: Any = None, **extra: Any):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.patch(url, data, format="json", **extra)

    def get(self, url: str, **extra: Any):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.get(url, **extra)

    # --- fixtures -------------------------------------------------------------

    def make_owner(self, email: str = "owner@acme.in", login: bool = True, **extra: Any) -> User:
        """An active, verified owner; authenticates the client with a real token."""
        extra.setdefault("full_name", "Asha Verma")
        extra.setdefault("email_verified_at", timezone.now())
        user = User.objects.create_user(email, STRONG_PASSWORD, **extra)
        if login:
            self.use_token(Token.objects.create(user=user).key)
        return user

    def make_company(self, user: User, **overrides: Any) -> Company:
        return Company.objects.create(owner=user, **{**COMPANY, **overrides})

    def use_token(self, token: str | None) -> None:
        if token:
            self.client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        else:
            self.client.credentials()

    def signup_and_verify(
        self, email: str = "asha@acme.in", full_name: str = "Asha Verma"
    ) -> dict[str, Any]:
        """Run the sign-up + code flow; returns the AuthResponse and authenticates the client."""
        response = self.post(
            "/api/auth/signup/",
            {"full_name": full_name, "email": email, "password": STRONG_PASSWORD, "accept_terms": True},
        )
        assert response.status_code == 201, response.content
        response = self.post("/api/auth/signup/verify/", {"email": email, "code": last_code(email)})
        assert response.status_code == 200, response.content
        body = response.json()
        self.use_token(body["token"])
        return body
