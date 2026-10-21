"""The series drip: Episode 1 on confirming, then one episode a day on the send days, in each reader's morning."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.core import mail
from django.test import override_settings
from django.utils import timezone
from rest_framework import status

from . import drip, newsletter
from .models import Article, NewsletterSubscriber, Series, SeriesSend
from .test_series import EMAIL, SERIES, SeriesTestCase
from .tests import STAFF

Sub = NewsletterSubscriber.Status
UTC = dt_timezone.utc
# Monday 26 Oct 2026, 10:00 in India (04:30 UTC); Wednesday 28 Oct is not a send day by default.
MONDAY_IST_10 = datetime(2026, 10, 26, 4, 30, tzinfo=UTC)


@EMAIL
@override_settings(ZINWE_SERIES_WELCOME_INLINE=True)
class DripTests(SeriesTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.show = self.make_series(drip_enabled=True)
        published = timezone.now() - timedelta(days=30)
        self.eps = []
        for n in range(1, 4):
            a = self.make_episode(self.show["id"], publish=False, title=f"Episode number {n}", newsletter_subject=f"Subject {n}")
            self.client.post(f"{STAFF}{a['id']}/publish/", {"at": (published + timedelta(minutes=n)).isoformat()}, format="json")
            self.eps.append(Article.objects.get(pk=a["id"]))

    def reader(self, email: str = "reader@example.com", tz: str = "Asia/Kolkata") -> NewsletterSubscriber:
        return NewsletterSubscriber.objects.create(email=email, status=Sub.ACTIVE, confirmed_at=timezone.now(), timezone=tz)

    def subjects(self) -> list[str]:
        return [m.subject for m in mail.outbox]

    def test_confirming_sends_episode_one_straight_away(self) -> None:
        self.anon()
        self.client.post("/api/blog/newsletter/subscribe/", {"email": "new@example.com", "timezone": "America/New_York"}, format="json")
        sub = NewsletterSubscriber.objects.get(email="new@example.com")
        self.assertEqual(sub.timezone, "America/New_York")
        mail.outbox.clear()
        self.client.post("/api/blog/newsletter/confirm/", {"token": sub.token}, format="json")
        self.assertEqual(self.subjects(), ["Subject 1"])
        self.assertIn(f"?k={sub.access_key}", mail.outbox[0].body)

    def test_a_bad_time_zone_is_ignored(self) -> None:
        newsletter.subscribe("tz@example.com", "", "Mars/Olympus")
        self.assertEqual(NewsletterSubscriber.objects.get(email="tz@example.com").timezone, "")

    def test_one_episode_a_send_day_in_order(self) -> None:
        self.reader()
        self.assertEqual(drip.send_due(MONDAY_IST_10).sent, 1)
        self.assertEqual(drip.send_due(MONDAY_IST_10 + timedelta(hours=2)).sent, 0)  # one a day
        self.assertEqual(drip.send_due(MONDAY_IST_10 + timedelta(days=1)).sent, 1)  # Tuesday
        self.assertEqual(drip.send_due(MONDAY_IST_10 + timedelta(days=2)).sent, 0)  # Wednesday: a break
        self.assertEqual(drip.send_due(MONDAY_IST_10 + timedelta(days=3)).sent, 1)  # Thursday
        self.assertEqual(self.subjects(), ["Subject 1", "Subject 2", "Subject 3"])
        result = drip.send_due(MONDAY_IST_10 + timedelta(days=4))  # Friday: nothing new released yet
        self.assertEqual((result.sent, result.waiting), (0, 1))

    def test_not_before_the_hour_in_the_readers_time_zone(self) -> None:
        self.reader(tz="America/New_York")  # Monday 04:30 UTC = Monday 00:30 in New York
        self.assertEqual(drip.send_due(MONDAY_IST_10).sent, 0)
        self.assertEqual(drip.send_due(MONDAY_IST_10 + timedelta(hours=9)).sent, 1)  # 09:30 in New York

    def test_scheduled_episodes_wait_for_their_release(self) -> None:
        later = self.make_episode(self.show["id"], publish=False, title="Not out yet")
        self.client.post(f"{STAFF}{later['id']}/publish/", {"at": (timezone.now() + timedelta(days=60)).isoformat()}, format="json")
        sub = self.reader()
        SeriesSend.objects.bulk_create([
            SeriesSend(series_id=self.show["id"], subscriber=sub, article=a, status="sent", local_date=MONDAY_IST_10.date() - timedelta(days=7))
            for a in self.eps
        ])
        self.assertEqual(drip.send_due(MONDAY_IST_10).sent, 0)

    def test_failed_email_is_retried_next_send_day_not_every_hour(self) -> None:
        self.reader()
        with mock.patch("z_blog.drip._deliver", side_effect=lambda row, sub, a: (setattr(row, "status", "failed"), row.save(), False)[2]):
            self.assertEqual(drip.send_due(MONDAY_IST_10).failed, 1)
        self.assertEqual(drip.send_due(MONDAY_IST_10 + timedelta(hours=1)).sent, 0)
        self.assertEqual(drip.send_due(MONDAY_IST_10 + timedelta(days=1)).sent, 1)
        self.assertEqual(self.subjects(), ["Subject 1"])

    def test_off_unsubscribed_and_unconfirmed_get_nothing(self) -> None:
        gone = self.reader("gone@example.com")
        newsletter.unsubscribe(gone.token)
        NewsletterSubscriber.objects.create(email="pending@example.com")
        self.assertEqual(drip.send_due(MONDAY_IST_10).sent, 0)
        self.reader()
        Series.objects.update(drip_enabled=False)
        self.assertEqual(drip.send_due(MONDAY_IST_10).sent, 0)

    def test_concurrent_runs_never_send_twice(self) -> None:
        sub = self.reader()
        first = drip._claim(Series.objects.get(), sub, self.eps[0], MONDAY_IST_10.date())
        self.assertIsNotNone(first)
        self.assertIsNone(drip._claim(Series.objects.get(), sub, self.eps[0], MONDAY_IST_10.date()))

    def test_admin_sets_the_schedule(self) -> None:
        r = self.client.patch(f"{SERIES}{self.show['id']}/", {"drip_weekdays": [0, 2, 4], "drip_hour": 8}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["drip_weekdays"], [0, 2, 4])
        self.assertEqual(r.json()["drip_schedule"], "Mon, Wed, Fri from 08:00 (reader's time)")
        r = self.client.patch(f"{SERIES}{self.show['id']}/", {"drip_weekdays": []}, format="json")
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_newsletter_card_shows_the_drip(self) -> None:
        body = self.client.get(f"{STAFF}{self.eps[0].id}/newsletter/").json()
        self.assertEqual(body["series_drip"]["subject"], "Subject 1")
        self.assertTrue(body["series_drip"]["enabled"])
