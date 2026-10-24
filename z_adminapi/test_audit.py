"""FIX-007: staff actions land in the audit trail (z_analytics Event "admin.*", actor = the staff member)."""
from __future__ import annotations

from unittest import mock

from django.utils import timezone
from rest_framework import status

from z_analytics.models import Event
from z_auth.models import Announcement, CustomerRemark, Plan, ReleaseNote, Subscription
from z_devices.models import AgentRelease

from .tests import SUBS, AdminApiTestCase

STAFF_EMAIL = "ops@zinwe.local"


class AuditTestCase(AdminApiTestCase):
    def audit(self, name: str) -> list[Event]:
        return list(Event.objects.filter(name=name).order_by("id"))

    def one(self, name: str) -> Event:
        rows = self.audit(name)
        self.assertEqual(len(rows), 1, f"{name}: {[e.name for e in Event.objects.all()]}")
        self.assertEqual(rows[0].actor_email, STAFF_EMAIL)
        return rows[0]


class CustomerAuditTests(AuditTestCase):
    def test_customer_delete_is_logged(self) -> None:
        owner = self.make_owner("gone@acme.in")
        self.assertEqual(self.client.delete(f"{SUBS}{owner.pk}/").status_code, status.HTTP_204_NO_CONTENT)
        event = self.one("admin.customer_deleted")
        self.assertEqual(event.properties, {"customer_id": owner.pk, "email": "gone@acme.in"})

    def test_refused_delete_is_not_logged(self) -> None:
        staff = self.make_owner("other@zinwe.local", is_staff=True)
        self.client.delete(f"{SUBS}{staff.pk}/")
        self.assertFalse(self.audit("admin.customer_deleted"))

    def test_remark_saved_and_cleared(self) -> None:
        owner = self.make_owner()
        self.client.post(f"{SUBS}{owner.pk}/remark/", {"text": "Pays late"}, format="json")
        saved = self.one("admin.remark_saved")
        self.assertEqual(saved.properties, {"customer_id": owner.pk})  # the note itself stays out of the log
        self.client.post(f"{SUBS}{owner.pk}/remark/", {"text": ""}, format="json")
        self.one("admin.remark_cleared")
        self.assertFalse(CustomerRemark.objects.exists())

    def test_payment_link_is_logged(self) -> None:
        owner = self.make_owner()
        sub = Subscription.objects.create(owner=owner, plan=Plan.objects.get(code="startups"))
        response = self.client.patch(
            f"/api/admin/subscriptions/{sub.pk}/", {"payment_link": "https://pay.skydo.com/x"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        event = self.one("admin.payment_link_set")
        self.assertEqual(event.properties, {"subscription_id": sub.pk, "customer_id": owner.pk,
                                            "payment_link": "https://pay.skydo.com/x"})


class AnnouncementAuditTests(AuditTestCase):
    URL = "/api/admin/announcements/"

    def test_create_edit_delete(self) -> None:
        created = self.client.post(self.URL, {"title": "Maintenance tonight"}, format="json")
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.content)
        pk = created.json()["id"]
        self.assertEqual(self.one("admin.announcement_created").properties["title"], "Maintenance tonight")

        self.client.patch(f"{self.URL}{pk}/", {"is_active": False}, format="json")
        self.assertEqual(self.one("admin.announcement_updated").properties, {"id": pk, "fields": ["is_active"]})

        self.assertEqual(self.client.delete(f"{self.URL}{pk}/").status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(self.one("admin.announcement_deleted").properties, {"id": pk, "title": "Maintenance tonight"})
        self.assertFalse(Announcement.objects.exists())


class ReleaseAuditTests(AuditTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.release = AgentRelease.objects.create(version="1.4.0", notes="Old", sha256="a" * 64)
        self.url = f"/api/admin/releases/{self.release.pk}/"

    def test_edit_is_logged_with_the_change(self) -> None:
        response = self.client.patch(self.url, {"required": False, "notes": " Faster "}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        event = self.one("admin.release_updated")
        self.assertEqual(event.properties, {"version": "1.4.0", "fields": ["notes", "required"]})
        self.release.refresh_from_db()
        self.assertEqual((self.release.required, self.release.notes), (False, "Faster"))

    def test_empty_patch_is_a_no_op(self) -> None:
        """An empty PATCH used to save the whole row (update_fields=None)."""
        with mock.patch.object(AgentRelease, "save") as save:
            response = self.client.patch(self.url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["version"], "1.4.0")
        save.assert_not_called()
        self.assertFalse(self.audit("admin.release_updated"))

    def test_only_changed_fields_are_saved(self) -> None:
        with mock.patch.object(AgentRelease, "save") as save:
            self.client.patch(self.url, {"is_active": True, "notes": "Old"}, format="json")  # both unchanged
        save.assert_not_called()
        self.assertFalse(self.audit("admin.release_updated"))
        with mock.patch.object(AgentRelease, "save") as save:
            self.client.patch(self.url, {"is_active": False, "notes": "Old"}, format="json")
        save.assert_called_once_with(update_fields=["is_active"])


class ReleaseNoteAuditTests(AuditTestCase):
    URL = "/api/admin/release-notes/"

    def test_write_publish_unpublish_delete(self) -> None:
        pk = self.client.post(self.URL, {"version": "1.2", "title": "Faster"}, format="json").json()["id"]
        self.assertEqual(self.one("admin.release_note_created").properties, {"id": pk, "version": "1.2"})

        self.client.patch(f"{self.URL}{pk}/", {"published_at": timezone.now().isoformat()}, format="json")
        self.one("admin.release_note_published")
        self.client.patch(f"{self.URL}{pk}/", {"title": "Faster payments"}, format="json")
        self.assertEqual(self.one("admin.release_note_updated").properties["fields"], ["title"])
        self.client.patch(f"{self.URL}{pk}/", {"published_at": None}, format="json")
        self.one("admin.release_note_unpublished")

        self.client.delete(f"{self.URL}{pk}/")
        self.one("admin.release_note_deleted")
        self.assertFalse(ReleaseNote.objects.exists())


class BlogAuditTests(AuditTestCase):
    ARTICLES = "/api/admin/blog/articles/"

    def test_article_lifecycle_is_logged(self) -> None:
        created = self.client.post(self.ARTICLES, {
            "title": "USB leaks", "type": "tutorial", "summary": "Short.", "body_html": "<p>" + "word " * 50 + "</p>",
        }, format="json")
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.content)
        pk = created.json()["id"]
        self.assertEqual(self.one("admin.blog_article_created").properties["id"], pk)

        self.client.patch(f"{self.ARTICLES}{pk}/", {"summary": "Shorter."}, format="json")
        self.one("admin.blog_article_updated")
        self.assertEqual(self.client.post(f"{self.ARTICLES}{pk}/publish/", {}, format="json").status_code, 200)
        self.one("admin.blog_article_published")
        self.client.post(f"{self.ARTICLES}{pk}/unpublish/", {}, format="json")
        self.one("admin.blog_article_unpublished")
        self.client.delete(f"{self.ARTICLES}{pk}/")
        self.assertEqual(self.one("admin.blog_article_deleted").properties, {"id": pk, "title": "USB leaks"})

    def test_subscriber_erase_is_logged_without_the_address(self) -> None:
        from z_blog.models import NewsletterSubscriber

        sub = NewsletterSubscriber.objects.create(email="reader@example.com")
        response = self.client.delete(f"/api/admin/blog/subscribers/{sub.pk}/")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        event = self.one("admin.newsletter_subscriber_erased")
        self.assertEqual(event.properties, {"subscriber_id": sub.pk})
        self.assertNotIn("reader@example.com", str(event.properties) + event.ref_type + str(event.ref_id))
