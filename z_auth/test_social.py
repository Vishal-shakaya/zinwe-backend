"""FR-38: Google and LinkedIn sign-in. The providers' HTTP calls are replaced; nothing leaves the machine."""
from __future__ import annotations

import base64
import json
import time
from unittest import mock

from django.test import override_settings
from rest_framework.test import APITestCase

from . import social
from .models import User

KEYS = {
    "GOOGLE_OAUTH_CLIENT_ID": "gid.apps.googleusercontent.com",
    "GOOGLE_OAUTH_CLIENT_SECRET": "gsecret",
    "LINKEDIN_CLIENT_ID": "lid",
    "LINKEDIN_CLIENT_SECRET": "lsecret",
    "LINKEDIN_REDIRECT_URIS": ["http://localhost:5173/auth/linkedin/callback"],
}
REDIRECT = "http://localhost:5173/auth/linkedin/callback"


def id_token(**claims: object) -> str:
    base = {"aud": KEYS["GOOGLE_OAUTH_CLIENT_ID"], "iss": "https://accounts.google.com", "exp": int(time.time()) + 600,
            "email": "asha@kitecrest.in", "email_verified": True, "name": "Asha Rao",
            "picture": "https://lh3.googleusercontent.com/a/photo"}
    base.update(claims)
    body = base64.urlsafe_b64encode(json.dumps(base).encode()).decode().rstrip("=")
    return f"h.{body}.s"


@override_settings(**KEYS)
class SocialConfigTests(APITestCase):
    def test_config_lists_enabled_providers_without_secrets(self) -> None:
        body = self.client.get("/api/auth/social/").json()
        self.assertTrue(body["google"]["enabled"])
        self.assertEqual(body["google"]["client_id"], KEYS["GOOGLE_OAUTH_CLIENT_ID"])
        self.assertEqual(body["linkedin"]["client_id"], "lid")
        self.assertNotIn("gsecret", json.dumps(body))
        self.assertNotIn("lsecret", json.dumps(body))

    @override_settings(LINKEDIN_CLIENT_SECRET="")
    def test_a_provider_without_its_secret_is_off(self) -> None:
        body = self.client.get("/api/auth/social/").json()
        self.assertFalse(body["linkedin"]["enabled"])
        self.assertEqual(body["linkedin"]["client_id"], "")


@override_settings(**KEYS)
class GoogleSignInTests(APITestCase):
    def post(self, token: str):
        with mock.patch.object(social, "_http", return_value={"id_token": token}) as http:
            response = self.client.post("/api/auth/social/google/", {"code": "c-1"}, format="json")
        return response, http

    def test_new_person_gets_an_active_verified_account_and_a_token(self) -> None:
        response, http = self.post(id_token())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["created"])
        self.assertTrue(body["token"])
        self.assertEqual(body["onboarding"]["step"], "company")
        user = User.objects.get(email="asha@kitecrest.in")
        self.assertTrue(user.is_active)
        self.assertIsNotNone(user.email_verified_at)
        self.assertFalse(user.has_usable_password())
        self.assertEqual((user.auth_provider, user.full_name, user.signup_source), ("google", "Asha Rao", "google"))
        self.assertEqual(body["user"]["avatar_url"], "https://lh3.googleusercontent.com/a/photo")
        sent = http.call_args.args[1]
        self.assertEqual((sent["redirect_uri"], sent["client_secret"]), ("postmessage", "gsecret"))

    def test_existing_account_signs_in_and_keeps_its_password(self) -> None:
        User.objects.create_user(email="Asha@Kitecrest.in", password="pw-123456789", full_name="Asha")
        response, _ = self.post(id_token())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["created"])
        user = User.objects.get()
        self.assertTrue(user.check_password("pw-123456789"))
        self.assertEqual(user.full_name, "Asha")

    def test_unfinished_email_signup_is_completed(self) -> None:
        User.objects.create_user(email="asha@kitecrest.in", password="pw-123456789", is_active=False)
        response, _ = self.post(id_token())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(User.objects.get().is_active)

    def test_paused_account_stays_paused(self) -> None:
        from django.utils import timezone
        User.objects.create_user(email="asha@kitecrest.in", is_active=False, email_verified_at=timezone.now())
        response, _ = self.post(id_token())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "account_paused")

    def test_token_for_another_app_is_refused(self) -> None:
        response, _ = self.post(id_token(aud="someone-else"))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.exists())

    def test_expired_or_unverified_tokens_are_refused(self) -> None:
        self.assertEqual(self.post(id_token(exp=int(time.time()) - 5))[0].status_code, 400)
        response, _ = self.post(id_token(email_verified=False))
        self.assertEqual(response.json()["code"], "email_not_verified")

    def test_unknown_provider_and_missing_code(self) -> None:
        self.assertEqual(self.client.post("/api/auth/social/github/", {"code": "x"}, format="json").status_code, 404)
        self.assertEqual(self.client.post("/api/auth/social/google/", {}, format="json").status_code, 400)


@override_settings(**KEYS)
class LinkedInSignInTests(APITestCase):
    USERINFO = {"sub": "abc", "email": "ravi@kitecrest.in", "email_verified": True, "name": "Ravi Kumar",
                "picture": "https://media.licdn.com/p.jpg"}

    def post(self, redirect: str = REDIRECT):
        calls = [{"access_token": "at-1", "id_token": "x"}, self.USERINFO]
        with mock.patch.object(social, "_http", side_effect=calls) as http:
            response = self.client.post("/api/auth/social/linkedin/", {"code": "c-2", "redirect_uri": redirect}, format="json")
        return response, http

    def test_signs_up_with_the_userinfo_email(self) -> None:
        response, http = self.post()
        self.assertEqual(response.status_code, 200)
        user = User.objects.get(email="ravi@kitecrest.in")
        self.assertEqual(user.auth_provider, "linkedin")
        token_call, info_call = http.call_args_list
        self.assertEqual(token_call.args[1]["redirect_uri"], REDIRECT)
        self.assertEqual(info_call.kwargs["bearer"], "at-1")

    def test_redirect_must_be_on_the_allow_list(self) -> None:
        response, http = self.post("https://evil.example/cb")
        self.assertEqual(response.json()["code"], "bad_redirect")
        http.assert_not_called()

    def test_provider_refusal_is_a_plain_400(self) -> None:
        with mock.patch.object(social, "_http", side_effect=social.SocialError("provider_rejected", "No.")):
            response = self.client.post("/api/auth/social/linkedin/", {"code": "c", "redirect_uri": REDIRECT}, format="json")
        self.assertEqual((response.status_code, response.json()["code"]), (400, "provider_rejected"))
