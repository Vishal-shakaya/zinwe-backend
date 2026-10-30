"""Blog: staff writing/publishing, cleaning, images, and the public site + SEO feeds."""
from __future__ import annotations

import shutil
import tempfile
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from z_auth.models import User

from .models import Article, BlogImage

STAFF = "/api/admin/blog/articles/"
PUBLIC = "/api/blog/articles/"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class BlogTestCase(APITestCase):
    def setUp(self) -> None:
        self.media = tempfile.mkdtemp()
        self.override = override_settings(MEDIA_ROOT=self.media, ZINWE_APP_URL="https://zinwe.test")
        self.override.enable()
        self.staff = User.objects.create_superuser("ops@zinwe.local", "ops-12345678", full_name="Ops Team")
        self.as_staff()

    def tearDown(self) -> None:
        self.override.disable()
        shutil.rmtree(self.media, ignore_errors=True)

    def as_staff(self) -> None:
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.get_or_create(user=self.staff)[0].key}")

    def anon(self) -> None:
        self.client.credentials()

    def create(self, **extra):  # type: ignore[no-untyped-def]
        data = {"title": "How Zinwe stops USB data leaks", "type": "tutorial",
                "summary": "A 5-minute guide.", "body_html": "<p>" + "word " * 450 + "</p>", **extra}
        response = self.client.post(STAFF, data, format="json")
        assert response.status_code == 201, response.content
        return response.json()


class StaffTests(BlogTestCase):
    def test_create_draft_with_slug_reading_time_and_author(self) -> None:
        body = self.create(tags="USB,  DPDP , usb")
        self.assertEqual(body["slug"], "how-zinwe-stops-usb-data-leaks")
        self.assertEqual(body["status"], "draft")
        self.assertEqual(body["reading_minutes"], 3)  # 450 words / 200 a minute
        self.assertEqual(body["author_name"], "Ops Team")
        self.assertEqual(body["tags"], "usb, dpdp")
        self.assertIsNone(body["published_at"])

    def test_every_type(self) -> None:
        for key in ("press_release", "tutorial", "newsletter", "new_release", "informative"):
            self.assertEqual(self.create(title=f"A {key}", type=key)["type"], key)
        self.assertEqual(self.client.post(STAFF, {"title": "x", "type": "gossip"}, format="json").status_code, 400)

    def test_duplicate_titles_get_unique_slugs(self) -> None:
        self.assertEqual(self.create()["slug"], "how-zinwe-stops-usb-data-leaks")
        self.assertEqual(self.create()["slug"], "how-zinwe-stops-usb-data-leaks-2")

    def test_body_is_cleaned(self) -> None:
        html = ('<h2>Setup</h2><p onclick="x()">Hi&nbsp;there<script>alert(1)</script></p>'
                '<img src="/api/blog/images/abc/" alt="shot" onerror="x()"><iframe src="https://evil"></iframe>'
                '<a href="javascript:alert(1)">bad</a><a href="https://zinwe.io">good</a>')
        body = self.create(body_html=html)["body_html"]
        for bad in ("<script", "onclick", "onerror", "<iframe", "javascript:", "&nbsp;"):
            self.assertNotIn(bad, body)
        self.assertIn("<h2>Setup</h2>", body)
        self.assertIn('<img src="/api/blog/images/abc/" alt="shot">', body)
        self.assertIn('href="https://zinwe.io"', body)

    def test_edit_keeps_slug_unless_changed(self) -> None:
        a = self.create()
        url = f"{STAFF}{a['id']}/"
        self.assertEqual(self.client.patch(url, {"title": "New title"}, format="json").json()["slug"], a["slug"])
        self.assertEqual(self.client.patch(url, {"slug": "Custom Slug!"}, format="json").json()["slug"], "custom-slug")

    def test_publish_unpublish_keeps_first_date(self) -> None:
        a = self.create()
        first = self.client.post(f"{STAFF}{a['id']}/publish/").json()
        self.assertEqual(first["status"], "published")
        self.client.post(f"{STAFF}{a['id']}/unpublish/")
        again = self.client.post(f"{STAFF}{a['id']}/publish/").json()
        self.assertEqual(again["published_at"], first["published_at"])

    def test_empty_article_cannot_be_published(self) -> None:
        a = self.create(body_html="<p> </p>")
        response = self.client.post(f"{STAFF}{a['id']}/publish/")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Write the article", response.json()["detail"])

    def test_list_filters_and_delete(self) -> None:
        a = self.create(title="Press one", type="press_release")
        self.create(title="Guide two")
        self.client.post(f"{STAFF}{a['id']}/publish/")
        self.assertEqual([r["title"] for r in self.client.get(STAFF, {"status": "published"}).json()], ["Press one"])
        self.assertEqual(len(self.client.get(STAFF, {"type": "tutorial"}).json()), 1)
        self.assertEqual(len(self.client.get(STAFF, {"q": "guide"}).json()), 1)
        self.assertEqual(self.client.delete(f"{STAFF}{a['id']}/").status_code, 204)
        self.assertEqual(Article.objects.count(), 1)

    def test_image_upload_and_cover(self) -> None:
        up = self.client.post("/api/admin/blog/images/", {"file": SimpleUploadedFile("x.png", PNG, "image/png")},
                              format="multipart")
        self.assertEqual(up.status_code, 201, up.content)
        img = up.json()
        self.assertTrue(img["url"].startswith("/api/blog/images/"))
        a = self.create(cover_image=img["id"], cover_alt="Dashboard")
        self.assertEqual(a["cover"]["url"], img["url"])
        self.anon()
        served = self.client.get(img["url"])
        self.assertEqual((served.status_code, served["Content-Type"]), (200, "image/png"))
        self.assertIn("immutable", served["Cache-Control"])

    def test_bad_images_refused(self) -> None:
        fake = self.client.post("/api/admin/blog/images/",
                                {"file": SimpleUploadedFile("x.png", b"<?php evil ?>", "image/png")}, format="multipart")
        self.assertEqual(fake.status_code, 400)
        big = self.client.post("/api/admin/blog/images/",
                               {"file": SimpleUploadedFile("x.png", PNG + b"0" * (5 * 1024 * 1024), "image/png")},
                               format="multipart")
        self.assertEqual(big.status_code, 400)
        self.assertEqual(BlogImage.objects.count(), 0)

    def test_staff_only(self) -> None:
        owner = User.objects.create_user("o@acme.in", "pw-12345678")
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=owner).key}")
        self.assertEqual(self.client.get(STAFF).status_code, status.HTTP_403_FORBIDDEN)
        self.anon()
        self.assertEqual(self.client.post(STAFF, {}, format="json").status_code, status.HTTP_401_UNAUTHORIZED)


class PublicTests(BlogTestCase):
    def publish(self, **extra):  # type: ignore[no-untyped-def]
        a = self.create(**extra)
        self.client.post(f"{STAFF}{a['id']}/publish/")
        return a

    def test_only_published_are_public(self) -> None:
        self.publish(title="Live guide")
        self.create(title="Secret draft")
        self.anon()
        body = self.client.get(PUBLIC).json()
        self.assertEqual([r["title"] for r in body["results"]], ["Live guide"])
        self.assertNotIn("body_html", body["results"][0])  # cards stay light
        self.assertEqual(self.client.get(f"{PUBLIC}secret-draft/").status_code, 404)

    def test_type_filter_counts_and_pages(self) -> None:
        for i in range(10):
            self.publish(title=f"Guide {i}")
        self.publish(title="Big news", type="press_release")
        self.anon()
        body = self.client.get(PUBLIC).json()
        self.assertEqual((body["count"], body["pages"], len(body["results"])), (11, 2, 9))
        counts = {t["key"]: t["count"] for t in body["types"]}
        self.assertEqual((counts["tutorial"], counts["press_release"], counts["newsletter"]), (10, 1, 0))
        self.assertEqual(len(self.client.get(PUBLIC, {"page": 2}).json()["results"]), 2)
        self.assertEqual([r["title"] for r in self.client.get(PUBLIC, {"type": "press_release"}).json()["results"]],
                         ["Big news"])
        self.assertEqual(self.client.get(PUBLIC, {"type": "nope"}).status_code, 400)

    def test_detail_seo_fallbacks_and_related(self) -> None:
        self.publish(title="Main", summary="Short summary.")
        self.publish(title="Other guide")
        self.publish(title="Release", type="new_release")
        self.anon()
        body = self.client.get(f"{PUBLIC}main/").json()
        # No meta description: a 140-160 character one from the summary, then the body, cut at a word.
        self.assertEqual((body["seo_title"], body["page_title"]), ("Main", "Main | Zinwe"))
        self.assertTrue(body["meta_description"].startswith("Short summary. word word"), body["meta_description"])
        self.assertTrue(140 <= len(body["meta_description"]) <= 160, len(body["meta_description"]))
        self.assertTrue(body["meta_description"].endswith("word…"))
        self.assertIn("word", body["body_html"])
        self.assertEqual([r["title"] for r in body["related"]][0], "Other guide")  # same type first
        self.assertEqual(len(body["related"]), 2)

    def test_scheduled_future_publish_date_stays_hidden(self) -> None:
        a = self.publish(title="Later")
        Article.objects.filter(pk=a["id"]).update(published_at=timezone.now() + timedelta(days=1))
        self.anon()
        self.assertEqual(self.client.get(PUBLIC).json()["count"], 0)

    def test_sitemap_and_rss(self) -> None:
        self.publish(title="Mapped & listed")
        self.create(title="Hidden draft")
        self.anon()
        sm = self.client.get("/api/blog/sitemap.xml")
        self.assertEqual(sm["Content-Type"], "application/xml")
        text = sm.content.decode()
        self.assertIn("<loc>https://zinwe.test/blog/mapped-listed</loc>", text)
        self.assertNotIn("hidden-draft", text)
        feed = self.client.get("/api/blog/rss.xml").content.decode()
        self.assertIn("<title>Mapped &amp; listed</title>", feed)
        self.assertIn("<category>Tutorial</category>", feed)
