"""
Social sign-in (FR-38): Google and LinkedIn.

Both providers end the same way as email sign-in: the person gets the normal AuthResponse (token + user +
onboarding), so onboarding, plans and payments work unchanged.

- Google: the website opens Google's pop-up (Google Identity Services, code flow) and sends us the one-time
  `code`. We swap it at Google's token endpoint with our client secret (redirect_uri "postmessage", Google's
  value for pop-ups) and read the ID token that comes back.
- LinkedIn: the website sends the person to LinkedIn ("Sign In with LinkedIn using OpenID Connect") and LinkedIn
  returns them to /auth/linkedin/callback with a `code`. We swap it for an access token and read /v2/userinfo.

The ID token is read from Google's token endpoint over TLS, which OpenID Connect Core §3.1.3.7 accepts in place
of a signature check; we still check the audience, issuer, expiry and that the email is verified.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.contrib.auth.models import update_last_login
from django.db import transaction
from django.utils import timezone

from z_analytics.services import track

from . import otp
from .models import User
from .services import AccountPaused, _after_signup_verified

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
LINKEDIN_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
LINKEDIN_USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
TIMEOUT_SECONDS = 10
PROVIDERS = ("google", "linkedin")


class SocialError(Exception):
    """The provider refused the code, or gave us no verified email. `code` is safe to show the client."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Identity:
    provider: str
    email: str
    name: str
    picture: str


# --- Configuration ---------------------------------------------------------------------------------------


def google_enabled() -> bool:
    return bool(settings.GOOGLE_OAUTH_CLIENT_ID and settings.GOOGLE_OAUTH_CLIENT_SECRET)


def linkedin_enabled() -> bool:
    return bool(settings.LINKEDIN_CLIENT_ID and settings.LINKEDIN_CLIENT_SECRET)


def public_config() -> dict[str, Any]:
    """What the website needs to show the buttons. Client ids are public; secrets never leave the server."""
    return {
        "google": {"enabled": google_enabled(), "client_id": settings.GOOGLE_OAUTH_CLIENT_ID if google_enabled() else ""},
        "linkedin": {
            "enabled": linkedin_enabled(),
            "client_id": settings.LINKEDIN_CLIENT_ID if linkedin_enabled() else "",
            "scope": "openid profile email",
        },
    }


# --- HTTP (one place, so tests replace it) ---------------------------------------------------------------


def _http(url: str, data: dict[str, str] | None = None, bearer: str = "") -> dict[str, Any]:
    """POST form data (or GET with a bearer token) and return the JSON body. Raises `SocialError`."""
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    request = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET")
    request.add_header("Accept", "application/json")
    if body is not None:
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
    if bearer:
        request.add_header("Authorization", f"Bearer {bearer}")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 -- fixed https URLs
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raise SocialError("provider_rejected", "The sign-in didn't go through. Please try again.") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise SocialError("provider_unreachable", "We couldn't reach the sign-in service. Please try again.") from exc


def _jwt_claims(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError) as exc:
        raise SocialError("provider_rejected", "The sign-in didn't go through. Please try again.") from exc


# --- Providers --------------------------------------------------------------------------------------------


def google_identity(code: str) -> Identity:
    if not google_enabled():
        raise SocialError("provider_disabled", "Google sign-in isn't available yet.")
    tokens = _http(GOOGLE_TOKEN_URL, {
        "code": code,
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
        "redirect_uri": "postmessage",
        "grant_type": "authorization_code",
    })
    claims = _jwt_claims(str(tokens.get("id_token") or ""))
    if (
        claims.get("aud") != settings.GOOGLE_OAUTH_CLIENT_ID
        or claims.get("iss") not in GOOGLE_ISSUERS
        or int(claims.get("exp") or 0) < time.time()
    ):
        raise SocialError("provider_rejected", "The sign-in didn't go through. Please try again.")
    return _identity("google", claims)


def linkedin_identity(code: str, redirect_uri: str) -> Identity:
    if not linkedin_enabled():
        raise SocialError("provider_disabled", "LinkedIn sign-in isn't available yet.")
    if redirect_uri not in settings.LINKEDIN_REDIRECT_URIS:
        raise SocialError("bad_redirect", "This sign-in link isn't allowed here.")
    tokens = _http(LINKEDIN_TOKEN_URL, {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": settings.LINKEDIN_CLIENT_ID,
        "client_secret": settings.LINKEDIN_CLIENT_SECRET,
    })
    access = str(tokens.get("access_token") or "")
    if not access:
        raise SocialError("provider_rejected", "The sign-in didn't go through. Please try again.")
    return _identity("linkedin", _http(LINKEDIN_USERINFO_URL, bearer=access))


def _identity(provider: str, claims: dict[str, Any]) -> Identity:
    email = otp.normalise_email(str(claims.get("email") or ""))
    verified = claims.get("email_verified")
    if not email or verified not in (True, "true", "True"):
        raise SocialError("email_not_verified", "Your account there has no verified email, so we can't sign you in with it.")
    picture = str(claims.get("picture") or "")
    return Identity(
        provider=provider,
        email=email,
        name=str(claims.get("name") or "").strip()[:150],
        picture=picture if picture.startswith("https://") else "",
    )


# --- Sign in or sign up -----------------------------------------------------------------------------------


def sign_in(identity: Identity) -> tuple[User, bool]:
    """
    Log in the account with this (provider-verified) email, or create it. Returns (user, created).

    An unfinished email sign-up becomes active: the provider has proved the address. A paused account stays
    paused (`AccountPaused`). Terms are accepted by continuing (the buttons say so, like the sign-up form).
    """
    now = timezone.now()
    with transaction.atomic():
        user = User.objects.select_for_update().filter(email__iexact=identity.email).first()
        created = user is None
        if user is None:
            user = User.objects.create_user(
                email=identity.email, password=None, full_name=identity.name, is_active=True,
                email_verified_at=now, signup_source=identity.provider,
                terms_accepted_at=now, terms_version=settings.ZINWE_LEGAL_VERSION,
            )
        else:
            if not user.is_active:
                if not user.is_pending_verification:
                    raise AccountPaused()
                user.is_active = True
            if user.email_verified_at is None:
                user.email_verified_at = now
            if not user.full_name and identity.name:
                user.full_name = identity.name
        user.auth_provider = identity.provider
        if identity.picture:
            user.avatar_url = identity.picture
        user.save()
        update_last_login(None, user)
        method = {"method": identity.provider}
        if created:
            transaction.on_commit(lambda: track("auth.signup_started", method, actor_email=user.email, ref=user))
            transaction.on_commit(lambda: _after_signup_verified(user))
        else:
            transaction.on_commit(lambda: track("auth.login", method, actor_email=user.email, ref=user))
    return user, created
