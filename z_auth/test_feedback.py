"""Owner feedback from the dashboard: POST/GET /api/auth/feedback/."""
from __future__ import annotations

from django.core.cache import cache
from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from .models import Feedback, User

URL = "/api/auth/feedback/"


class FeedbackTests(APITestCase):
    def setUp(self) -> None:
        cache.clear()  # throttle counters
        self.owner = self.login_as("owner@acme.in")

    def login_as(self, email: str) -> User:
        user = User.objects.create_user(email, "pw-12345678", email_verified_at=timezone.now())
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        return user

    def test_send_feedback(self) -> None:
        response = self.client.post(URL, {"rating": 8, "message": "  Clear alerts, want more reports  ", "page": "/app"},
                                    format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        body = response.json()
        self.assertEqual((body["rating"], body["message"], body["page"]), (8, "Clear alerts, want more reports", "/app"))
        saved = Feedback.objects.get()
        self.assertEqual((saved.owner, saved.rating), (self.owner, 8))

    def test_message_is_optional(self) -> None:
        response = self.client.post(URL, {"rating": 10}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json()["message"], "")

    def test_rating_must_be_1_to_10(self) -> None:
        for bad in (0, 11, "x", None):
            with self.subTest(rating=bad):
                payload = {} if bad is None else {"rating": bad}
                response = self.client.post(URL, payload, format="json")
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(response.json()["rating"], ["Pick a rating from 1 to 10."])
        self.assertFalse(Feedback.objects.exists())

    def test_message_length_capped(self) -> None:
        response = self.client.post(URL, {"rating": 5, "message": "x" * 2001}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_history_is_own_only(self) -> None:
        self.client.post(URL, {"rating": 4, "message": "mine"}, format="json")
        Feedback.objects.create(owner=User.objects.create_user("other@acme.in", "pw-12345678"), rating=9)
        body = self.client.get(URL).json()
        self.assertEqual([(r["rating"], r["message"]) for r in body], [(4, "mine")])

    def test_owner_cannot_choose_who_it_is_from(self) -> None:
        other = User.objects.create_user("other@acme.in", "pw-12345678")
        self.client.post(URL, {"rating": 7, "owner": other.id}, format="json")
        self.assertEqual(Feedback.objects.get().owner, self.owner)

    def test_requires_auth(self) -> None:
        self.client.credentials()
        self.assertEqual(self.client.post(URL, {"rating": 7}, format="json").status_code, status.HTTP_401_UNAUTHORIZED)

    def test_throttled_after_20_per_hour(self) -> None:
        for _ in range(20):
            self.client.post(URL, {"rating": 7}, format="json")
        self.assertEqual(self.client.post(URL, {"rating": 7}, format="json").status_code,
                         status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(self.client.get(URL).status_code, status.HTTP_200_OK)  # reading is not throttled
