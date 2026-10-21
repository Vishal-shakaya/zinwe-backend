"""The series as a growth loop: episodes open per member as their emails arrive, share links (referrals),
reads per episode, Zinwe sign-ups attributed to the newsletter, and the series on blog articles."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone

from django.core import mail
from django.test import override_settings
from django.utils import timezone

from z_auth.models import User

from . import drip, newsletter
from .models import Article, NewsletterSubscriber, SeriesRead, SeriesSend
from .test_series import EMAIL, SERIES, SeriesTestCase
from .tests import STAFF

Sub = NewsletterSubscriber.Status
UTC = dt_timezone.utc
MONDAY_IST_10 = datetime(2026, 10, 26, 4, 30, tzinfo=UTC)


@EMAIL
@override_settings(ZINWE_SERIES_WELCOME_INLINE=True, ZINWE_POSTAL_ADDRESS="SKYTECHNO, 1 Test Road, New Delhi 110001")
class GrowthTests(SeriesTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.show = self.make_series(drip_enabled=True)
        past = timezone.now() - timedelta(days=30)
        self.eps = []
        for n in range(1, 4):
            a = self.make_episode(self.show["id"], publish=False, title=f"Episode number {n}", tags="usb drives, pen drive")
            self.client.post(f"{STAFF}{a['id']}/publish/", {"at": (past + timedelta(minutes=n)).isoformat()}, format="json")
            self.eps.append(Article.objects.get(pk=a["id"]))
        self.sub = NewsletterSubscriber.objects.create(
            email="reader@example.com", status=Sub.ACTIVE, confirmed_at=timezone.now(), timezone="Asia/Kolkata"
        )

    def url(self, a: Article) -> str:
        return f"/api/blog/series/the-last-file/{a.slug}/"

    def test_episodes_open_as_the_members_emails_arrive(self) -> None:
        self.anon()
        first = self.client.get(self.url(self.eps[0]), {"k": self.sub.access_key}).json()
        self.assertFalse(first["locked"])  # Episode 1 is always theirs
        second = self.client.get(self.url(self.eps[1]), {"k": self.sub.access_key}).json()
        self.assertTrue(second["locked"])
        self.assertTrue(second["member_locked"])
        self.assertIsNotNone(second["unlocks_on"])
        self.assertEqual(second["body_html"], "")
        SeriesSend.objects.create(series_id=self.show["id"], subscriber=self.sub, article=self.eps[1], status="sent",
                                  local_date=timezone.localdate())
        self.assertFalse(self.client.get(self.url(self.eps[1]), {"k": self.sub.access_key}).json()["locked"])
        series = self.client.get("/api/blog/series/the-last-file/", {"k": self.sub.access_key}).json()
        self.assertEqual([e["unlocked"] for e in series["episodes"]], [True, True, False])
        self.assertEqual(series["reader"]["progress"], {"unlocked": 2, "released": 3})

    def test_with_the_drip_off_members_read_everything(self) -> None:
        self.client.patch(f"{SERIES}{self.show['id']}/", {"drip_enabled": False}, format="json")
        self.anon()
        self.assertFalse(self.client.get(self.url(self.eps[2]), {"k": self.sub.access_key}).json()["locked"])

    def test_unlock_dates_follow_the_send_days(self) -> None:
        # Episode 1 went out on confirming (last week). Monday 10:00 India, nothing sent today: Episode 2 arrives
        # today, Episode 3 tomorrow (Tuesday).
        SeriesSend.objects.create(series_id=self.show["id"], subscriber=self.sub, article=self.eps[0], status="sent",
                                  local_date=MONDAY_IST_10.date() - timedelta(days=4))
        self.assertEqual(drip.unlocks_on(self.eps[1], self.sub, MONDAY_IST_10), MONDAY_IST_10.date())
        self.assertEqual(drip.unlocks_on(self.eps[2], self.sub, MONDAY_IST_10), MONDAY_IST_10.date() + timedelta(days=1))

    def test_reads_are_counted_once_per_member(self) -> None:
        self.anon()
        self.client.get(self.url(self.eps[0]), {"k": self.sub.access_key, "via": "email"})
        self.client.get(self.url(self.eps[0]), {"k": self.sub.access_key})
        row = SeriesRead.objects.get(subscriber=self.sub, article=self.eps[0])
        self.assertEqual((row.reads, row.via_email), (2, True))

    def test_share_link_credits_the_member(self) -> None:
        self.anon()
        self.client.post("/api/blog/newsletter/subscribe/", {"email": "friend@example.com", "ref": self.sub.ref_code}, format="json")
        friend = NewsletterSubscriber.objects.get(email="friend@example.com")
        self.assertEqual(friend.referred_by, self.sub)
        newsletter.subscribe("self@example.com", "", "", "nope-code")
        self.assertIsNone(NewsletterSubscriber.objects.get(email="self@example.com").referred_by)

    def test_episode_email_has_next_reply_share_and_start_free(self) -> None:
        row = drip._claim(self.eps[0].series, self.sub, self.eps[0], timezone.localdate())
        drip._deliver(row, self.sub, self.eps[0])
        html = mail.outbox[-1].alternatives[0][0]
        body = mail.outbox[-1].body
        self.assertIn("Episode 02 · Episode number 2", body)
        self.assertIn("Reply and tell us what you found", body)
        self.assertIn(f"ref={self.sub.ref_code}", html)
        self.assertIn("/signup?plan=free&amp;utm_source=newsletter&amp;utm_medium=email&amp;utm_campaign=tlf&amp;utm_content=ep01", html)
        self.assertIn("utm_content=ep01", html)
        self.assertIn("1 Test Road", body)

    def test_signup_source_is_kept(self) -> None:
        self.anon()
        r = self.client.post("/api/auth/signup/", {
            "full_name": "Asha Rao", "email": "asha@example.com", "password": "a-long-pass-123", "accept_terms": True,
            "source": "tlf:ep02:email",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(User.objects.get(email="asha@example.com").signup_source, "tlf:ep02:email")
        self.as_staff()
        stats = self.client.get(f"{SERIES}{self.show['id']}/stats/").json()
        self.assertEqual(stats["signups"], 1)
        self.assertEqual([e["signups"] for e in stats["episodes"]], [0, 1, 0])

    def test_bad_signup_source_is_refused(self) -> None:
        self.anon()
        r = self.client.post("/api/auth/signup/", {
            "full_name": "Asha Rao", "email": "asha2@example.com", "password": "a-long-pass-123", "accept_terms": True,
            "source": "<script>",
        }, format="json")
        self.assertEqual(r.status_code, 400)

    def test_blog_articles_point_to_the_closest_episode(self) -> None:
        a = self.create(title="Stop data leaks from USB drives", tags="usb drives")
        self.client.post(f"{STAFF}{a['id']}/publish/", {}, format="json")
        self.anon()
        pick = self.client.get(f"/api/blog/articles/{a['slug']}/").json()["series_pick"]
        self.assertEqual(pick["series_slug"], "the-last-file")
        self.assertIn(pick["slug"], [e.slug for e in self.eps])
