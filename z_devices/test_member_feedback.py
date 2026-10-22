"""FR-14: a member sends feedback on Zinwe from their Zinwe window; the Zinwe team reads it, the owner doesn't."""
from __future__ import annotations

from rest_framework import status

from z_auth.models import Feedback, User

from .tests import DeviceTestCase

URL = "/api/devices/feedback/"


class MemberFeedbackTests(DeviceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.as_device(self.enroll().json()["device_token"])

    def test_member_feedback_is_filed_under_the_company(self) -> None:
        response = self.client.post(URL, {"rating": 8, "message": "  Clear and quick.  "}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        fb = Feedback.objects.get()
        self.assertEqual((fb.owner, fb.source, fb.member, fb.rating), (self.owner, "member", self.member, 8))
        self.assertEqual((fb.message, fb.device.hostname, fb.page), ("Clear and quick.", "ACCOUNTS-PC-01", "Zinwe window"))

    def test_rating_is_required_and_bounded(self) -> None:
        for body in ({}, {"rating": 0}, {"rating": 11}, {"rating": "x"}):
            self.assertEqual(self.client.post(URL, body, format="json").status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Feedback.objects.exists())

    def test_needs_a_device_token(self) -> None:
        self.client.credentials()
        self.assertEqual(self.client.post(URL, {"rating": 5}, format="json").status_code, status.HTTP_401_UNAUTHORIZED)
        self.as_owner()  # an owner's login isn't a PC
        self.assertIn(self.client.post(URL, {"rating": 5}, format="json").status_code,
                      (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    def test_owner_does_not_see_member_feedback(self) -> None:
        self.client.post(URL, {"rating": 3, "message": "Too many pop-ups"}, format="json")
        self.as_owner()
        self.client.post("/api/auth/feedback/", {"rating": 9}, format="json")
        mine = self.client.get("/api/auth/feedback/").json()
        self.assertEqual([r["rating"] for r in mine], [9])

    def test_staff_see_who_sent_it(self) -> None:
        self.client.post(URL, {"rating": 6}, format="json")
        staff = User.objects.create_superuser("ops@zinwe.local", "ops-12345678")
        self.client.force_authenticate(staff)
        self.client.credentials()
        rows = self.client.get("/api/admin/feedback/").json()["rows"]
        row = next(r for r in rows if r["source"] == "member")
        self.assertEqual(row["member"], {"name": "Asha Rao", "hostname": "ACCOUNTS-PC-01"})
        self.assertEqual(row["customer"]["email"], "owner@acme.in")

    def test_throttled(self) -> None:
        for _ in range(10):
            self.client.post(URL, {"rating": 5}, format="json")
        self.assertEqual(self.client.post(URL, {"rating": 5}, format="json").status_code, status.HTTP_429_TOO_MANY_REQUESTS)
