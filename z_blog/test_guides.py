"""Free e-guides: a public page per guide, a members' download, instant delivery on confirm, matching to
articles and episodes, staff management and numbers."""
from __future__ import annotations

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone

from z_auth.models import User

from . import newsletter
from .models import GuideDownload, NewsletterSubscriber
from .tests import BlogTestCase, STAFF

Sub = NewsletterSubscriber.Status
GUIDES = "/api/admin/blog/guides/"
PDF = b"%PDF-1.7\n1 0 obj << /Type /Page >> endobj 2 0 obj << /Type /Page >> endobj\n" + b"0" * 200
EMAIL = override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")


class GuideTestCase(BlogTestCase):
    def make_guide(self, publish: bool = True, **extra):  # type: ignore[no-untyped-def]
        data = {
            "title": "How to protect your business from online scams", "subtitle": "Spot, verify, protect payments.",
            "summary": "A playbook for small teams.", "tags": "scams, payments, invoice, phishing",
            "campaign": "guide-scams", "contents": [{"title": "How scams work", "note": "The four-step chain"}],
            "highlights": [{"value": "8", "label": "Scam patterns"}], **extra,
        }
        r = self.client.post(GUIDES, data, format="json")
        assert r.status_code == 201, r.content
        g = r.json()
        r = self.client.post(f"{GUIDES}{g['id']}/file/", {"file": SimpleUploadedFile("scams.pdf", PDF)}, format="multipart")
        assert r.status_code == 200, r.content
        if publish:
            g = self.client.patch(f"{GUIDES}{g['id']}/", {"status": "published"}, format="json").json()
        return g

    def member(self, email: str = "reader@example.com") -> NewsletterSubscriber:
        return NewsletterSubscriber.objects.create(email=email, status=Sub.ACTIVE, confirmed_at=timezone.now())


class StaffGuideTests(GuideTestCase):
    def test_create_upload_publish(self) -> None:
        g = self.make_guide()
        self.assertEqual(g["slug"], "how-to-protect-your-business-from-online-scams")
        self.assertEqual(g["status"], "published")
        self.assertTrue(g["has_file"])
        self.assertEqual(g["page_count"], 2)

    def test_cannot_publish_without_a_pdf(self) -> None:
        r = self.client.post(GUIDES, {"title": "Draft guide", "status": "published"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_only_pdfs(self) -> None:
        g = self.make_guide(publish=False)
        r = self.client.post(f"{GUIDES}{g['id']}/file/", {"file": SimpleUploadedFile("x.exe", b"MZ nope")}, format="multipart")
        self.assertEqual(r.status_code, 400)

    def test_staff_only(self) -> None:
        self.anon()
        self.assertEqual(self.client.get(GUIDES).status_code, 401)


class PublicGuideTests(GuideTestCase):
    def test_page_is_public_download_is_for_members(self) -> None:
        g = self.make_guide()
        self.anon()
        page = self.client.get(f"/api/blog/guides/{g['slug']}/").json()
        self.assertFalse(page["member"])
        self.assertIsNone(page["download_url"])
        self.assertEqual(page["contents"][0]["title"], "How scams work")
        self.assertEqual(self.client.get(f"/api/blog/guides/{g['slug']}/download/").status_code, 403)
        sub = self.member()
        page = self.client.get(f"/api/blog/guides/{g['slug']}/", {"k": sub.access_key}).json()
        self.assertTrue(page["member"])
        r = self.client.get(page["download_url"], {"k": sub.access_key})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(b"".join(r.streaming_content), PDF)
        self.client.get(page["download_url"], {"k": sub.access_key})
        self.assertEqual(GuideDownload.objects.get(subscriber=sub).downloads, 2)

    def test_drafts_are_hidden(self) -> None:
        g = self.make_guide(publish=False)
        self.anon()
        self.assertEqual(self.client.get(f"/api/blog/guides/{g['slug']}/").status_code, 404)
        self.assertEqual(self.client.get("/api/blog/guides/").json()["results"], [])

    def test_articles_get_the_closest_guide(self) -> None:
        self.make_guide()
        a = self.create(title="Stop invoice scams before you pay", tags="invoice, payments")
        self.client.post(f"{STAFF}{a['id']}/publish/", {}, format="json")
        self.anon()
        pick = self.client.get(f"/api/blog/articles/{a['slug']}/").json()["guide_pick"]
        self.assertEqual(pick["campaign"], "guide-scams")

    def test_prerender_and_sitemap(self) -> None:
        g = self.make_guide()
        self.anon()
        html = self.client.get(f"/blog/guides/{g['slug']}").content.decode()
        self.assertIn("How scams work", html)
        self.assertIn('"@type": "Book"', html)
        self.assertEqual(self.client.get("/blog/guides").status_code, 200)
        self.assertEqual(self.client.get("/blog/guides/nope").status_code, 404)
        self.assertIn(f"/blog/guides/{g['slug']}</loc>", self.client.get("/api/blog/sitemap.xml").content.decode())

    def test_guides_slug_is_reserved_for_articles(self) -> None:
        self.assertEqual(self.create(title="Guides")["slug"], "guides-article")


@EMAIL
class GuideSignupTests(GuideTestCase):
    def test_confirmation_email_delivers_the_guide(self) -> None:
        g = self.make_guide()
        newsletter.subscribe("new@example.com", f"/blog/guides/{g['slug']}")
        message = mail.outbox[-1]
        self.assertEqual(message.subject, "Confirm your email to download your free Zinwe guide")
        self.assertIn("How to protect your business from online scams", message.body)
        self.assertIn("Confirm and download the guide", message.alternatives[0][0])

    def test_member_on_a_new_device_gets_a_link_to_the_guide(self) -> None:
        g = self.make_guide()
        sub = self.member()
        newsletter.subscribe(sub.email, f"/blog/guides/{g['slug']}")
        self.assertIn(f"/blog/guides/{g['slug']}?k={sub.access_key}", mail.outbox[-1].body)

    def test_guide_numbers(self) -> None:
        g = self.make_guide()
        NewsletterSubscriber.objects.create(email="a@example.com", source=f"/blog/guides/{g['slug']}")
        User.objects.create_user(email="owner@example.com", password="x-long-pass-1", signup_source="guide-scams:guide:site")
        stats = self.client.get(f"{GUIDES}{g['id']}/").json()["stats"]
        self.assertEqual((stats["joined_on_page"], stats["signups"]), (1, 1))
