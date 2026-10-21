"""Members-only series: series + episodes, the reader key that unlocks them, episode files, the
episode email and the pre-rendered pages."""
from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework import status

from . import newsletter
from .models import Article, NewsletterSubscriber, Series
from .tests import BlogTestCase, STAFF

Sub = NewsletterSubscriber.Status
SERIES = "/api/admin/blog/series/"
PDF = b"%PDF-1.4\n" + b"0" * 200
EMAIL = override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
SECRET = "Priya turned to Alex"  # a line from the members-only part


class SeriesTestCase(BlogTestCase):
    def make_series(self, **extra):  # type: ignore[no-untyped-def]
        data = {"title": "The Last File", "tagline": "One small mistake can change everything.",
                "planned_episodes": 30, "seasons": "1 | The Warning | Trust\n2 | The Invisible Risk | Uncertainty",
                "status": "published", **extra}
        r = self.client.post(SERIES, data, format="json")
        assert r.status_code == 201, r.content
        return r.json()

    def make_episode(self, series_id: int, publish: bool = True, **extra):  # type: ignore[no-untyped-def]
        data = {"series": series_id, "teaser_html": "<p>4:42 PM. Tuesday. The phone rang.</p>",
                "body_html": f"<p>{SECRET}. " + "word " * 300 + "</p>", **extra}
        a = self.create(**data)
        if publish:
            r = self.client.post(f"{STAFF}{a['id']}/publish/", {}, format="json")
            assert r.status_code == 200, r.content
            a = r.json()
        return a

    def member(self, email: str = "reader@example.com") -> NewsletterSubscriber:
        return NewsletterSubscriber.objects.create(email=email, status=Sub.ACTIVE)


class StaffSeriesTests(SeriesTestCase):
    def test_create_with_seasons_from_text_lines(self) -> None:
        s = self.make_series()
        self.assertEqual(s["slug"], "the-last-file")
        self.assertEqual(s["seasons"][1], {"number": 2, "title": "The Invisible Risk", "theme": "Uncertainty"})

    def test_episode_numbers_are_given_in_order_and_must_be_unique(self) -> None:
        s = self.make_series()
        first = self.make_episode(s["id"], publish=False)
        second = self.make_episode(s["id"], publish=False, title="Everyone Had Access")
        self.assertEqual((first["episode"], second["episode"]), (1, 2))
        r = self.client.patch(f"{STAFF}{second['id']}/", {"episode": 1}, format="json")
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already", r.json()["detail"])

    def test_an_episode_needs_its_public_opening_to_publish(self) -> None:
        s = self.make_series()
        a = self.make_episode(s["id"], publish=False, teaser_html="")
        r = self.client.post(f"{STAFF}{a['id']}/publish/", {}, format="json")
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_leaving_a_series_clears_the_episode_fields(self) -> None:
        s = self.make_series()
        a = self.make_episode(s["id"], publish=False, free_preview=True)
        body = self.client.patch(f"{STAFF}{a['id']}/", {"series": None}, format="json").json()
        self.assertEqual((body["series"], body["episode"], body["free_preview"]), (None, None, False))

    def test_series_with_live_episodes_cant_be_deleted(self) -> None:
        s = self.make_series()
        self.make_episode(s["id"])
        self.assertEqual(self.client.delete(f"{SERIES}{s['id']}/").status_code, status.HTTP_400_BAD_REQUEST)

    def test_staff_only(self) -> None:
        self.anon()
        self.assertEqual(self.client.get(SERIES).status_code, status.HTTP_401_UNAUTHORIZED)


class PublicSeriesTests(SeriesTestCase):
    def test_episodes_stay_out_of_the_blog_list(self) -> None:
        s = self.make_series()
        ep = self.make_episode(s["id"])
        plain = self.create(title="An ordinary article")
        self.client.post(f"{STAFF}{plain['id']}/publish/", {}, format="json")
        self.anon()
        self.assertEqual([a["slug"] for a in self.client.get("/api/blog/articles/").json()["results"]], [plain["slug"]])
        self.assertEqual(self.client.get(f"/api/blog/articles/{ep['slug']}/").status_code, 404)

    def test_series_page_lists_released_and_upcoming(self) -> None:
        s = self.make_series()
        self.make_episode(s["id"])
        later = self.make_episode(s["id"], publish=False, title="Everyone Had Access")
        self.client.post(f"{STAFF}{later['id']}/publish/", {"at": (timezone.now() + timedelta(days=7)).isoformat()}, format="json")
        self.anon()
        body = self.client.get("/api/blog/series/the-last-file/").json()
        self.assertEqual([e["episode"] for e in body["episodes"]], [1])
        self.assertEqual(body["upcoming"][0]["title"], "Everyone Had Access")
        self.assertNotIn("summary", body["upcoming"][0])
        self.assertIsNone(body["reader"])
        self.assertEqual(self.client.get("/api/blog/series/").json()["results"][0]["released"], 1)

    def test_draft_series_is_hidden(self) -> None:
        s = self.make_series(status="draft")
        a = self.make_episode(s["id"])
        self.anon()
        self.assertEqual(self.client.get("/api/blog/series/the-last-file/").status_code, 404)
        self.assertEqual(self.client.get(f"/api/blog/series/the-last-file/{a['slug']}/").status_code, 404)

    def test_locked_without_a_reader_key_and_open_with_one(self) -> None:
        s = self.make_series()
        a = self.make_episode(s["id"])
        url = f"/api/blog/series/the-last-file/{a['slug']}/"
        self.anon()
        locked = self.client.get(url).json()
        self.assertTrue(locked["locked"])
        self.assertEqual(locked["body_html"], "")
        self.assertNotIn(SECRET, str(locked))
        self.assertIn("The phone rang", locked["teaser_html"])
        self.assertEqual(locked["label"], "THE LAST FILE · EPISODE 01")

        sub = self.member()
        opened = self.client.get(url, {"k": sub.access_key}).json()
        self.assertFalse(opened["locked"])
        self.assertIn(SECRET, opened["body_html"])
        self.assertEqual(opened["reader"]["email"], "r•••@example.com")

        # The unsubscribe token is not a reader key, and a cancelled membership closes the episode again.
        self.assertTrue(self.client.get(url, {"k": sub.token}).json()["locked"])
        newsletter.unsubscribe(sub.token)
        self.assertTrue(self.client.get(url, {"k": sub.access_key}).json()["locked"])

    def test_free_preview_is_open_to_everyone(self) -> None:
        s = self.make_series()
        a = self.make_episode(s["id"], free_preview=True)
        self.anon()
        body = self.client.get(f"/api/blog/series/the-last-file/{a['slug']}/").json()
        self.assertFalse(body["locked"])
        self.assertIn(SECRET, body["body_html"])

    def test_previous_and_next(self) -> None:
        s = self.make_series()
        one = self.make_episode(s["id"])
        two = self.make_episode(s["id"], title="Everyone Had Access")
        self.anon()
        body = self.client.get(f"/api/blog/series/the-last-file/{two['slug']}/").json()
        self.assertEqual(body["previous"]["slug"], one["slug"])
        self.assertIsNone(body["next"])


class FileTests(SeriesTestCase):
    def upload(self, article_id: int, content: bytes = PDF, name: str = "Case file 01.pdf"):  # type: ignore[no-untyped-def]
        return self.client.post(
            f"{STAFF}{article_id}/attachments/",
            {"file": SimpleUploadedFile(name, content), "title": "Case file (PDF)"},
            format="multipart",
        )

    def test_members_only_file(self) -> None:
        s = self.make_series()
        a = self.make_episode(s["id"])
        r = self.upload(a["id"])
        self.assertEqual(r.status_code, 201, r.content)
        att = r.json()["attachments"][0]
        self.assertEqual((att["filename"], att["content_type"]), ("case-file-01.pdf", "application/pdf"))

        self.anon()
        locked = self.client.get(f"/api/blog/series/the-last-file/{a['slug']}/").json()
        self.assertEqual(locked["attachments"], [{"title": "Case file (PDF)", "content_type": "application/pdf", "size": len(PDF)}])
        self.assertEqual(self.client.get(att["url"]).status_code, status.HTTP_403_FORBIDDEN)
        r = self.client.get(att["url"], {"k": self.member().access_key})
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment;", r["Content-Disposition"])
        self.assertEqual(b"".join(r.streaming_content), PDF)

    def test_file_of_an_unpublished_episode_is_not_found(self) -> None:
        s = self.make_series()
        a = self.make_episode(s["id"], publish=False)
        att = self.upload(a["id"]).json()["attachments"][0]
        self.anon()
        self.assertEqual(self.client.get(att["url"], {"k": self.member().access_key}).status_code, 404)

    def test_only_pdf_images_and_zip(self) -> None:
        s = self.make_series()
        a = self.make_episode(s["id"], publish=False)
        r = self.upload(a["id"], b"MZ\x90\x00 an exe", "tool.exe")
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rename_and_remove(self) -> None:
        s = self.make_series()
        a = self.make_episode(s["id"], publish=False)
        att = self.upload(a["id"]).json()["attachments"][0]
        body = self.client.patch(f"/api/admin/blog/attachments/{att['id']}/", {"title": "Checklist"}, format="json").json()
        self.assertEqual(body["attachments"][0]["title"], "Checklist")
        body = self.client.delete(f"/api/admin/blog/attachments/{att['id']}/").json()
        self.assertEqual(body["attachments"], [])


@EMAIL
class MembershipTests(SeriesTestCase):
    def test_confirm_hands_over_the_reader_key_and_where_they_came_from(self) -> None:
        self.anon()
        self.client.post("/api/blog/newsletter/subscribe/", {"email": "new@example.com", "source": "/blog/series/the-last-file"}, format="json")
        sub = NewsletterSubscriber.objects.get(email="new@example.com")
        body = self.client.post("/api/blog/newsletter/confirm/", {"token": sub.token}, format="json").json()
        self.assertEqual(body["access_key"], sub.access_key)
        self.assertEqual(body["source"], "/blog/series/the-last-file")

    def test_confirm_email_names_the_live_series(self) -> None:
        self.make_series()
        newsletter.subscribe("new@example.com")
        self.assertEqual(mail.outbox[-1].subject, "Confirm your free Zinwe membership")
        self.assertIn("The Last File", mail.outbox[-1].body)

    def test_a_member_asking_again_gets_their_reading_link_once_an_hour(self) -> None:
        sub = self.member()
        newsletter.subscribe("reader@example.com", "/blog/series/the-last-file/the-file?x=1")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Your Zinwe members' reading link")
        self.assertIn(f"https://zinwe.test/blog/series/the-last-file/the-file?k={sub.access_key}", mail.outbox[0].body)
        newsletter.subscribe("reader@example.com", "/blog/series/the-last-file")
        self.assertEqual(len(mail.outbox), 1)

    def test_reading_link_never_points_off_the_series(self) -> None:
        self.assertEqual(newsletter.reading_page("https://evil.example/x"), "/blog/series")
        self.assertEqual(newsletter.reading_page("/legal/terms"), "/blog/series")
        self.assertEqual(newsletter.reading_page("/blog/series/the-last-file/"), "/blog/series/the-last-file")


@EMAIL
class EpisodeEmailTests(SeriesTestCase):
    def test_episode_email_is_a_teaser_with_the_members_link(self) -> None:
        s = self.make_series()
        a = self.make_episode(
            s["id"], title="The File That Almost Destroyed Everything",
            newsletter_subject="The file was correct. The recipient wasn't.",
        )
        sub = self.member()
        article = Article.objects.get(pk=a["id"])
        with self.assertRaises(newsletter.BlogError):  # episodes go out through the drip, never as a one-off
            newsletter.queue(article, "", self.staff)
        from . import drip

        row = drip._claim(article.series, sub, article, timezone.localdate())
        self.assertTrue(drip._deliver(row, sub, article))
        message = mail.outbox[-1]
        self.assertEqual(message.subject, "The file was correct. The recipient wasn't.")
        html = message.alternatives[0][0]
        self.assertIn("READ THE FULL CASE FILE", html)
        self.assertIn(f"/blog/series/the-last-file/{a['slug']}?k={sub.access_key}", html)
        self.assertIn("The phone rang", html)
        self.assertNotIn(SECRET, html)
        self.assertNotIn(SECRET, message.body)
        self.assertIn(sub.token, message.extra_headers["List-Unsubscribe"])

    def test_test_send_has_no_reader_key(self) -> None:
        s = self.make_series()
        a = self.make_episode(s["id"])
        newsletter.send_test(Article.objects.get(pk=a["id"]), "ops@zinwe.local")
        self.assertNotIn("?k=", mail.outbox[-1].body)


class PrerenderTests(SeriesTestCase):
    def test_episode_page_shows_only_the_public_opening(self) -> None:
        s = self.make_series()
        a = self.make_episode(s["id"])
        self.anon()
        r = self.client.get(f"/blog/series/the-last-file/{a['slug']}")
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn("The phone rang", html)
        self.assertNotIn(SECRET, html)
        self.assertIn('"isAccessibleForFree": false', html)
        self.assertIn('class="ep-members"', html)

    def test_series_pages_and_sitemap(self) -> None:
        s = self.make_series()
        a = self.make_episode(s["id"])
        self.anon()
        self.assertEqual(self.client.get("/blog/series").status_code, 200)
        self.assertIn("CreativeWorkSeries", self.client.get("/blog/series/the-last-file").content.decode())
        self.assertEqual(self.client.get("/blog/series/nope").status_code, 404)
        xml = self.client.get("/api/blog/sitemap.xml").content.decode()
        self.assertIn(f"/blog/series/the-last-file/{a['slug']}</loc>", xml)

    def test_series_slug_is_reserved_for_articles(self) -> None:
        self.assertEqual(self.create(title="Series")["slug"], "series-article")

    def test_scheduled_episode_is_not_reachable_early(self) -> None:
        s = self.make_series()
        a = self.make_episode(s["id"], publish=False)
        at = timezone.now() + timedelta(days=1)
        self.client.post(f"{STAFF}{a['id']}/publish/", {"at": at.isoformat()}, format="json")
        self.anon()
        url = f"/api/blog/series/the-last-file/{a['slug']}/"
        self.assertEqual(self.client.get(url).status_code, 404)
        with mock.patch("django.utils.timezone.now", return_value=at + timedelta(minutes=1)):
            self.assertEqual(self.client.get(url).status_code, 200)
        self.assertTrue(Series.objects.exists())
