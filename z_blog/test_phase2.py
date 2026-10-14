"""Blog phase 2: scheduled publishing, the newsletter (sign-up, confirm, unsubscribe, sending), and
search-engine pre-rendering of /blog pages."""
from __future__ import annotations

import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.core import mail
from django.test import override_settings
from django.utils import timezone
from rest_framework import status

from . import newsletter
from .models import Article, NewsletterDelivery, NewsletterIssue, NewsletterSubscriber
from .tests import BlogTestCase, STAFF

Sub = NewsletterSubscriber.Status

EMAIL = override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")


class SchedulingTests(BlogTestCase):
    def test_schedule_hides_until_the_time_then_goes_live_on_its_own(self) -> None:
        a = self.create()
        at = timezone.now() + timedelta(days=2)
        body = self.client.post(f"{STAFF}{a['id']}/publish/", {"at": at.isoformat()}, format="json").json()
        self.assertEqual(body["state"], "scheduled")
        self.assertEqual(self.client.get(f"{STAFF}?status=scheduled").json()[0]["id"], a["id"])
        self.assertEqual(self.client.get(f"{STAFF}?status=published").json(), [])
        self.anon()
        self.assertEqual(self.client.get(f"/api/blog/articles/{a['slug']}/").status_code, 404)
        with mock.patch("django.utils.timezone.now", return_value=at + timedelta(minutes=1)):
            self.assertEqual(self.client.get(f"/api/blog/articles/{a['slug']}/").status_code, 200)

    def test_publish_now_and_states(self) -> None:
        a = self.create()
        self.assertEqual(a["state"], "draft")
        body = self.client.post(f"{STAFF}{a['id']}/publish/", {}, format="json").json()
        self.assertEqual(body["state"], "published")
        body = self.client.post(f"{STAFF}{a['id']}/unpublish/").json()
        self.assertEqual(body["state"], "draft")

    def test_bad_date_refused(self) -> None:
        a = self.create()
        r = self.client.post(f"{STAFF}{a['id']}/publish/", {"at": "next tuesday"}, format="json")
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)


@EMAIL
class SignupTests(BlogTestCase):
    def setUp(self) -> None:
        super().setUp()
        from django.core.cache import cache

        cache.clear()  # throttle counters
        self.anon()

    def subscribe(self, email: str = "Reader@Example.com"):  # type: ignore[no-untyped-def]
        return self.client.post("/api/blog/newsletter/subscribe/", {"email": email, "source": "/blog"}, format="json")

    def test_double_opt_in(self) -> None:
        self.assertEqual(self.subscribe().status_code, status.HTTP_202_ACCEPTED)
        sub = NewsletterSubscriber.objects.get()
        self.assertEqual((sub.email, sub.status, sub.source), ("reader@example.com", Sub.PENDING, "/blog"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(f"https://zinwe.test/newsletter?confirm={sub.token}", mail.outbox[0].body)
        r = self.client.post("/api/blog/newsletter/confirm/", {"token": sub.token}, format="json")
        self.assertEqual(r.json()["status"], "active")
        sub.refresh_from_db()
        self.assertIsNotNone(sub.confirmed_at)

    def test_no_resend_spam_and_no_list_leak(self) -> None:
        self.subscribe()
        self.subscribe()  # within the hour: no second email
        self.assertEqual(len(mail.outbox), 1)
        newsletter.confirm(NewsletterSubscriber.objects.get().token)
        r = self.subscribe()  # already active: same answer, no email
        self.assertEqual((r.status_code, r.json()), (202, {"status": "check_inbox"}))
        self.assertEqual(len(mail.outbox), 1)

    def test_unsubscribe_page_and_one_click(self) -> None:
        self.subscribe()
        sub = NewsletterSubscriber.objects.get()
        newsletter.confirm(sub.token)
        r = self.client.post("/api/blog/newsletter/unsubscribe/", {"token": sub.token}, format="json")
        self.assertEqual(r.json()["status"], "unsubscribed")
        # An old confirm link can't re-subscribe them.
        r = self.client.post("/api/blog/newsletter/confirm/", {"token": sub.token}, format="json")
        self.assertEqual(r.status_code, 400)
        # Signing up again restarts double opt-in.
        self.subscribe()
        self.assertEqual(NewsletterSubscriber.objects.get().status, Sub.PENDING)
        # RFC 8058 one-click (form body, no CSRF token) from the mail client.
        r = self.client.post(f"/api/blog/newsletter/unsubscribe/{sub.token}/", {"List-Unsubscribe": "One-Click"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(NewsletterSubscriber.objects.get().status, Sub.UNSUBSCRIBED)

    def test_bad_tokens_and_emails(self) -> None:
        self.assertEqual(self.client.post("/api/blog/newsletter/confirm/", {"token": "nope"}, format="json").status_code, 400)
        self.assertEqual(self.subscribe("not-an-email").status_code, 400)

    def test_signup_throttled(self) -> None:
        for i in range(10):
            self.subscribe(f"r{i}@example.com")
        self.assertEqual(self.subscribe("r99@example.com").status_code, status.HTTP_429_TOO_MANY_REQUESTS)


@EMAIL
class SendTests(BlogTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.subs = [
            NewsletterSubscriber.objects.create(email=f"s{i}@example.com", status=Sub.ACTIVE) for i in range(3)
        ]
        NewsletterSubscriber.objects.create(email="pending@example.com", status=Sub.PENDING)
        NewsletterSubscriber.objects.create(email="gone@example.com", status=Sub.UNSUBSCRIBED)

    def published(self, **extra):  # type: ignore[no-untyped-def]
        a = self.create(**extra)
        self.client.post(f"{STAFF}{a['id']}/publish/", {}, format="json")
        return a

    def queue(self, article_id: int, **body):  # type: ignore[no-untyped-def]
        with self.captureOnCommitCallbacks(execute=False):  # no background thread in tests
            return self.client.post(f"{STAFF}{article_id}/newsletter/", body, format="json")

    def test_send_once_to_confirmed_subscribers_with_unsubscribe(self) -> None:
        a = self.published(type="newsletter", body_html='<h2>Hi</h2><p>Body <a href="/plans">plans</a></p><p>' + "w " * 50 + "</p>")
        state = self.client.get(f"{STAFF}{a['id']}/newsletter/").json()
        self.assertEqual((state["audience"], state["issue"], state["full_article"]), (3, None, True))
        r = self.queue(a["id"], subject="October news")
        self.assertEqual(r.status_code, 201, r.content)
        result = newsletter.send_pending()
        self.assertEqual((result.sent, result.failed), (3, 0))
        self.assertEqual(sorted(m.to[0] for m in mail.outbox), ["s0@example.com", "s1@example.com", "s2@example.com"])
        msg = mail.outbox[0]
        self.assertEqual(msg.subject, "October news")
        sub = NewsletterSubscriber.objects.get(email=msg.to[0])
        self.assertEqual(msg.extra_headers["List-Unsubscribe"], f"<https://zinwe.test/api/blog/newsletter/unsubscribe/{sub.token}/>")
        self.assertEqual(msg.extra_headers["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        html = msg.alternatives[0][0]
        self.assertIn(f"https://zinwe.test/newsletter?unsubscribe={sub.token}", html)
        self.assertIn('href="https://zinwe.test/plans"', html)  # site links made absolute
        self.assertIn('<h2 style="', html)  # inline styles for mail clients
        # Done, and never again.
        issue = self.client.get(f"{STAFF}{a['id']}/newsletter/").json()["issue"]
        self.assertEqual((issue["status"], issue["sent"], issue["total"]), ("sent", 3, 3))
        self.assertEqual(newsletter.send_pending().sent, 0)
        self.assertEqual(self.queue(a["id"]).status_code, 400)
        self.assertEqual(len(mail.outbox), 3)

    def test_other_types_send_a_teaser(self) -> None:
        a = self.published(type="tutorial")
        self.queue(a["id"])
        newsletter.send_pending()
        msg = mail.outbox[0]
        self.assertEqual(msg.subject, "How Zinwe stops USB data leaks")
        self.assertIn("Read the tutorial", msg.alternatives[0][0])
        self.assertNotIn("word word word", msg.body)

    def test_scheduled_article_waits_for_go_live(self) -> None:
        a = self.create()
        at = timezone.now() + timedelta(hours=3)
        self.client.post(f"{STAFF}{a['id']}/publish/", {"at": at.isoformat()}, format="json")
        self.assertEqual(self.queue(a["id"]).status_code, 201)
        self.assertTrue(self.client.get(f"{STAFF}{a['id']}/newsletter/").json()["issue"]["waiting_for_publish"])
        result = newsletter.send_pending()
        self.assertEqual((result.sent, len(result.waiting)), (0, 1))
        result = newsletter.send_pending(now=at + timedelta(minutes=1))
        self.assertEqual(result.sent, 3)

    def test_publish_now_sends_a_queued_email_straight_away(self) -> None:
        a = self.create()
        self.client.post(f"{STAFF}{a['id']}/publish/", {"at": (timezone.now() + timedelta(days=1)).isoformat()}, format="json")
        self.queue(a["id"])
        with mock.patch("z_blog.newsletter.start_background") as start, self.captureOnCommitCallbacks(execute=True):
            self.client.post(f"{STAFF}{a['id']}/publish/", {"at": timezone.now().isoformat()}, format="json")
        start.assert_called_once_with(NewsletterIssue.objects.get().pk)

    def test_drafts_and_empty_audience_refused(self) -> None:
        a = self.create()
        self.assertIn("Publish or schedule", self.queue(a["id"]).json()["detail"])
        NewsletterSubscriber.objects.filter(status=Sub.ACTIVE).update(status=Sub.UNSUBSCRIBED)
        b = self.published()
        self.assertIn("Nobody has confirmed", self.queue(b["id"]).json()["detail"])

    def test_unsubscribed_mid_send_is_skipped_and_failures_retry(self) -> None:
        a = self.published()
        self.queue(a["id"])
        issue = NewsletterIssue.objects.get()
        newsletter._start(issue, timezone.now())
        self.subs[0].status = Sub.UNSUBSCRIBED
        self.subs[0].save()
        with mock.patch("z_notification.channels.email.EmailMultiAlternatives.send", side_effect=OSError("smtp down")):
            result = newsletter.send_pending()
        self.assertEqual((result.sent, result.failed, result.skipped), (0, 2, 1))
        self.assertEqual(NewsletterIssue.objects.get().status, "sent")  # nothing left in flight
        with self.captureOnCommitCallbacks(execute=False):
            body = self.client.post(f"{STAFF}{a['id']}/newsletter/retry/").json()
        self.assertEqual(body["issue"]["pending"], 2)
        self.assertEqual(newsletter.send_pending().sent, 2)
        self.assertEqual(NewsletterDelivery.objects.filter(status="sent").count(), 2)

    def test_a_claimed_delivery_is_not_sent_twice(self) -> None:
        a = self.published()
        self.queue(a["id"])
        issue = NewsletterIssue.objects.get()
        newsletter._start(issue, timezone.now())
        NewsletterDelivery.objects.filter(subscriber=self.subs[0]).update(status="sending")  # another sender has it
        self.assertEqual(newsletter.send_pending().sent, 2)

    def test_queue_starts_a_background_send_when_live(self) -> None:
        a = self.published()
        with mock.patch("z_blog.newsletter.start_background") as start, self.captureOnCommitCallbacks(execute=True):
            self.client.post(f"{STAFF}{a['id']}/newsletter/", {}, format="json")
        start.assert_called_once_with(NewsletterIssue.objects.get().pk)

    def test_test_send_to_me(self) -> None:
        a = self.create(type="newsletter")
        r = self.client.post(f"{STAFF}{a['id']}/newsletter/test/")
        self.assertEqual(r.json(), {"sent_to": "ops@zinwe.local"})
        self.assertEqual(mail.outbox[0].subject, "[Test] How Zinwe stops USB data leaks")
        self.assertNotIn("List-Unsubscribe", mail.outbox[0].extra_headers)

    def test_subscribers_admin(self) -> None:
        body = self.client.get("/api/admin/blog/subscribers/").json()
        self.assertEqual(body["counts"], {"pending": 1, "active": 3, "unsubscribed": 1})
        self.assertEqual(len(self.client.get("/api/admin/blog/subscribers/?status=active").json()["rows"]), 3)
        self.assertEqual(len(self.client.get("/api/admin/blog/subscribers/?q=gone").json()["rows"]), 1)
        sid = self.subs[0].pk
        self.assertEqual(self.client.delete(f"/api/admin/blog/subscribers/{sid}/").status_code, 204)
        self.anon()
        self.assertEqual(self.client.get("/api/admin/blog/subscribers/").status_code, 401)


INDEX = """<!doctype html>
<html lang="en">
  <head>
    <title>Zinwe — Security, simplified</title>
    <meta name="description" content="Default description." />
    <meta property="og:type" content="website" />
    <meta property="og:title" content="Zinwe" />
    <meta property="og:image" content="https://zinwe.io/brand/og-image.png" />
    <script type="module" crossorigin src="/assets/index-abc.js"></script>
  </head>
  <body>
    <div id="root"></div>
  </body>
</html>"""


class PrerenderTests(BlogTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.index = Path(tempfile.mkdtemp()) / "index.html"
        self.index.write_text(INDEX, encoding="utf-8")
        self.index_override = override_settings(ZINWE_FRONTEND_INDEX=str(self.index))
        self.index_override.enable()

    def tearDown(self) -> None:
        self.index_override.disable()
        super().tearDown()

    def test_article_page_has_seo_tags_and_content_inside_the_app_shell(self) -> None:
        a = self.create(seo_title="Stop USB leaks | Zinwe", meta_description='Guide "for" small teams', body_html="<h2>Step 1</h2><p>Do this.</p>")
        self.client.post(f"{STAFF}{a['id']}/publish/", {}, format="json")
        self.anon()
        r = self.client.get(f"/blog/{a['slug']}")
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn("<title>Stop USB leaks | Zinwe — Zinwe</title>", html)
        self.assertIn('<meta name="description" content="Guide &quot;for&quot; small teams" data-prerender-content="Default description."', html)
        self.assertIn('<meta property="og:type" content="article" data-prerender-content="website"', html)
        self.assertIn(f'<link rel="canonical" href="https://zinwe.test/blog/{a["slug"]}" data-prerender />', html)
        self.assertIn('"@type": "BlogPosting"', html)
        self.assertIn('<div id="root"><div class="prerender">', html)
        self.assertIn("<h2>Step 1</h2><p>Do this.</p>", html)
        self.assertIn('src="/assets/index-abc.js"', html)  # the app still loads and takes over
        self.assertEqual(self.client.get(f"/blog/{a['slug']}/").status_code, 200)

    def test_list_page_and_missing_article(self) -> None:
        a = self.create()
        self.client.post(f"{STAFF}{a['id']}/publish/", {}, format="json")
        draft = self.create(title="Secret draft")
        self.anon()
        html = self.client.get("/blog").content.decode()
        self.assertIn(f'<a href="/blog/{a["slug"]}">', html)
        self.assertNotIn("Secret draft", html)
        self.assertIn('<link rel="canonical" href="https://zinwe.test/blog" data-prerender />', html)
        r = self.client.get(f"/blog/{draft['slug']}")
        self.assertEqual(r.status_code, 404)
        self.assertIn('content="noindex"', r.content.decode())

    def test_script_in_json_ld_cannot_break_out(self) -> None:
        a = self.create(title="</script><script>alert(1)</script>")
        self.client.post(f"{STAFF}{a['id']}/publish/", {}, format="json")
        html = self.client.get(f"/blog/{Article.objects.get(pk=a['id']).slug}").content.decode()
        self.assertNotIn("<script>alert(1)", html)

    def test_works_without_a_built_website(self) -> None:
        a = self.create()
        self.client.post(f"{STAFF}{a['id']}/publish/", {}, format="json")
        with override_settings(ZINWE_FRONTEND_INDEX=str(self.index.with_name("missing.html"))):
            r = self.client.get(f"/blog/{a['slug']}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("How Zinwe stops USB data leaks", r.content.decode())
