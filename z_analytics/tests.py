from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from z_auth.models import EarlyAccessRegistration, User

from .models import Event
from .services import track

SUMMARY_URL = "/api/analytics/summary/"


class TrackTests(TestCase):
    def test_track_creates_event(self) -> None:
        event = track("demo.clicked", {"button": "cta"}, actor_email="A@B.co", ref=("thing", 3))
        self.assertEqual(event.name, "demo.clicked")
        self.assertEqual(event.properties, {"button": "cta"})
        self.assertEqual(event.actor_email, "a@b.co")
        self.assertEqual((event.ref_type, event.ref_id), ("thing", "3"))

    def test_track_never_raises(self) -> None:
        with (
            mock.patch.object(Event.objects, "create", side_effect=RuntimeError("db down")),
            self.assertLogs("zinwe.analytics", "ERROR"),
        ):
            self.assertIsNone(track("demo.clicked"))


class SummaryTests(APITestCase):
    def setUp(self) -> None:
        self.staff = User.objects.create_user("staff@zinwe.io", "pw-staff-123456", is_staff=True)
        self.member = User.objects.create_user("member@acme.in", "pw-member-123456")

    def make_registration(self, email: str, team_size: str, concerns: list[str]) -> EarlyAccessRegistration:
        return EarlyAccessRegistration.objects.create(
            name="X", email=email, company="Co", team_size=team_size, concerns=concerns
        )

    def test_anonymous_is_rejected(self) -> None:
        # 401 (not 403) because TokenAuthentication supplies a WWW-Authenticate challenge.
        self.assertEqual(self.client.get(SUMMARY_URL).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_non_staff_is_forbidden(self) -> None:
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.get(SUMMARY_URL).status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_gets_summary(self) -> None:
        self.make_registration("a@x.in", "1-10", ["usb", "email"])
        self.make_registration("b@x.in", "1-10", ["usb"])
        old = self.make_registration("c@x.in", "50+", [])
        EarlyAccessRegistration.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=30)
        )
        track("early_access.registered")

        self.client.force_authenticate(self.staff)
        response = self.client.get(SUMMARY_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.json(),
            {
                "early_access": {
                    "total": 3,
                    "last_7_days": 2,
                    "by_team_size": {"1-10": 2, "11-25": 0, "26-50": 0, "50+": 1},
                    "by_concern": {"usb": 2, "remote": 0, "installs": 0, "email": 1, "compliance": 0},
                },
                "events": {"total": 1},
            },
        )
