"""Testimonials: written in the admin app, shown on Home, and never public without a note of how the person agreed."""
from __future__ import annotations

from typing import Any

from rest_framework import status

from ._testing import PortalTestCase
from .models import Feedback, Testimonial, User

PUBLIC = "/api/auth/testimonials/"
ADMIN = "/api/admin/testimonials/"
FEEDBACK = "/api/admin/feedback/"


def body(**kw: Any) -> dict[str, Any]:
    return {"quote": "Calm office at last.", "rating": 9, "name": "Asha", "role": "Founder", "company": "Acme", **kw}


class TestimonialTests(PortalTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.staff = User.objects.create_superuser("ops@zinwe.local", "ops-12345678")
        self.client.force_authenticate(self.staff)

    def add(self, **kw: Any) -> dict[str, Any]:
        response = self.post(ADMIN, body(**kw))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        return response.json()

    def test_a_new_one_is_private_until_published(self) -> None:
        row = self.add(permission="Email on 3 Nov")
        self.assertFalse(row["is_published"])
        self.assertEqual(self.get(PUBLIC).json(), [])
        self.assertTrue(self.patch(f"{ADMIN}{row['id']}/", {"is_published": True}).json()["is_published"])
        self.assertEqual([t["name"] for t in self.get(PUBLIC).json()], ["Asha"])
        self.patch(f"{ADMIN}{row['id']}/", {"is_published": False})
        self.assertEqual(self.get(PUBLIC).json(), [])

    def test_only_8_to_10_can_be_saved(self) -> None:
        for rating in (0, 7, 11):
            response = self.post(ADMIN, body(rating=rating))
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, rating)
        self.assertEqual(self.post(ADMIN, body(rating=7)).json()["rating"], ["Testimonials show ratings of 8 to 10."])
        for rating in (8, 10):
            self.add(rating=rating)

    def test_publishing_needs_a_note_of_how_they_agreed(self) -> None:
        refused = self.post(ADMIN, body(is_published=True))
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("permission", refused.json())
        url = f"{ADMIN}{self.add()['id']}/"
        self.assertEqual(self.patch(url, {"is_published": True}).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.patch(url, {"permission": "  ", "is_published": True}).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.patch(url, {"permission": "WhatsApp, 4 Nov", "is_published": True}).status_code, status.HTTP_200_OK)
        self.assertEqual(self.patch(url, {"permission": ""}).status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_public_list_says_nothing_about_permission_or_the_feedback(self) -> None:
        feedback = Feedback.objects.create(owner=self.staff, rating=9, message="Lovely")
        self.add(permission="Email on 3 Nov", is_published=True, source_feedback=feedback.pk)
        row = self.get(PUBLIC).json()[0]
        self.assertEqual(set(row), {"id", "kind", "quote", "rating", "name", "role", "company"})

    def test_home_gets_the_newest_few(self) -> None:
        for n in range(Testimonial.PUBLIC_LIMIT + 2):
            self.add(name=f"P{n}", permission="Email", is_published=True)
        rows = self.get(PUBLIC).json()
        self.assertEqual(len(rows), Testimonial.PUBLIC_LIMIT)
        self.assertEqual(rows[0]["name"], f"P{Testimonial.PUBLIC_LIMIT + 1}")

    def test_good_feedback_can_be_used_and_stays_as_it_was(self) -> None:
        feedback = Feedback.objects.create(owner=self.staff, rating=10, message="Lovely")
        self.assertIsNone(self.get(FEEDBACK).json()["rows"][0]["testimonial_id"])
        row = self.add(quote="Lovely", rating=10, source_feedback=feedback.pk)
        self.assertEqual(self.get(FEEDBACK).json()["rows"][0]["testimonial_id"], row["id"])
        self.assertEqual(self.post(ADMIN, body(source_feedback=feedback.pk)).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.client.delete(f"{ADMIN}{row['id']}/").status_code, status.HTTP_204_NO_CONTENT)
        feedback.refresh_from_db()
        self.assertEqual(feedback.message, "Lovely")
        self.assertIsNone(self.get(FEEDBACK).json()["rows"][0]["testimonial_id"])

    def test_owners_cannot_read_or_write_them_here(self) -> None:
        self.client.force_authenticate(user=None)
        self.make_owner()
        self.assertEqual(self.get(ADMIN).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.post(ADMIN, body()).status_code, status.HTTP_403_FORBIDDEN)
