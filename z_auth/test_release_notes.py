"""FR-21: release notes ("What's new"), written in the admin app and shown on the website and dashboard."""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from rest_framework import status

from ._testing import PortalTestCase
from .models import ReleaseNote, User

PUBLIC = "/api/auth/whats-new/"
ADMIN = "/api/admin/release-notes/"


class ReleaseNoteTests(PortalTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.staff = User.objects.create_superuser("ops@zinwe.local", "ops-12345678")

    def test_only_published_notes_are_public(self) -> None:
        now = timezone.now()
        ReleaseNote.objects.create(version="1.0", title="Hello", published_at=now - timedelta(days=30))
        ReleaseNote.objects.create(version="1.1", title="Faster payments", body="## New\n- QR on request",
                                   published_at=now - timedelta(minutes=1))
        ReleaseNote.objects.create(version="1.2", title="Draft")
        ReleaseNote.objects.create(version="1.3", title="Later", published_at=now + timedelta(days=1))
        rows = self.get(PUBLIC).json()
        self.assertEqual([r["version"] for r in rows], ["1.1", "1.0"])
        self.assertEqual(rows[0]["body"], "## New\n- QR on request")

    def test_staff_write_publish_and_unpublish(self) -> None:
        self.client.force_authenticate(self.staff)
        created = self.post(ADMIN, {"version": " 1.1 ", "title": "Faster payments", "body": "- QR"})
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.content)
        note = created.json()
        self.assertEqual((note["version"], note["status"]), ("1.1", "draft"))
        url = f"{ADMIN}{note['id']}/"
        live = self.patch(url, {"published_at": timezone.now().isoformat()}).json()
        self.assertEqual(live["status"], "live")
        self.assertEqual(len(self.get(PUBLIC).json()), 1)
        self.assertEqual(self.patch(url, {"published_at": None}).json()["status"], "draft")
        self.assertEqual(self.get(PUBLIC).json(), [])
        self.assertEqual(self.post(ADMIN, {"version": "", "title": "x"}).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.client.delete(url).status_code, status.HTTP_204_NO_CONTENT)

    def test_owners_cannot_write_notes(self) -> None:
        self.make_owner()
        self.assertEqual(self.post(ADMIN, {"version": "9", "title": "x"}).status_code, status.HTTP_403_FORBIDDEN)
