"""Guide follow-ups: off until approved; then one step at a time from the first download, from 09:00 local,
never more than one Zinwe email a day, and never twice."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone

from django.core import mail

from . import guide_followups as fu
from . import newsletter
from .models import Guide, GuideDownload, GuideFollowupSend, NewsletterSubscriber, SeriesSend
from .test_guides import EMAIL, GUIDES, GuideTestCase

UTC = dt_timezone.utc
# Day of the download: Monday 26 Oct 2026, 10:00 in India (04:30 UTC).
D0 = datetime(2026, 10, 26, 4, 30, tzinfo=UTC)
STEPS = [
    {"day": 0, "subject": "Your guide, and where to start", "body_html": "<p>Start with page 5.</p>", "cta_label": "Open the guide", "cta_url": "/blog/guides/x"},
    {"day": 3, "subject": "One worksheet this week", "body_html": "<p>Do the data map.</p>"},
    {"day": 7, "subject": "A story about this", "body_html": "<p>Read Episode 1.</p>", "cta_label": "Read it", "cta_url": "/blog/series/the-last-file/ep"},
    {"day": 12, "subject": "See it on your own PCs", "body_html": "<p>Start free.</p>", "cta_label": "Start Zinwe free", "cta_url": "/signup?plan=free"},
]


@EMAIL
class FollowupTests(GuideTestCase):
    def setUp(self) -> None:
        super().setUp()
        g = self.make_guide(followups=STEPS)
        self.guide = Guide.objects.get(pk=g["id"])
        self.sub = self.member()
        self.sub.timezone = "Asia/Kolkata"
        self.sub.save()
        dl = GuideDownload.objects.create(guide=self.guide, subscriber=self.sub)
        GuideDownload.objects.filter(pk=dl.pk).update(first_at=D0 - timedelta(hours=1))
        mail.outbox.clear()

    def enable(self) -> None:
        self.client.patch(f"{GUIDES}{self.guide.pk}/", {"followups_enabled": True}, format="json")
        Guide.objects.filter(pk=self.guide.pk).update(followups_enabled_at=D0 - timedelta(days=1))

    def test_downloads_before_approval_get_nothing(self) -> None:
        self.client.patch(f"{GUIDES}{self.guide.pk}/", {"followups_enabled": True}, format="json")
        self.assertIsNotNone(Guide.objects.get(pk=self.guide.pk).followups_enabled_at)
        Guide.objects.filter(pk=self.guide.pk).update(followups_enabled_at=D0 + timedelta(days=1))  # approved after the download
        self.assertEqual(fu.send_due(D0 + timedelta(days=30)).sent, 0)  # downloaded before it was switched on

    def test_nothing_goes_out_until_approved(self) -> None:
        self.assertEqual(fu.send_due(D0).sent, 0)
        self.assertEqual(mail.outbox, [])

    def test_steps_follow_the_days_one_at_a_time(self) -> None:
        self.enable()
        self.assertEqual(fu.send_due(D0).sent, 1)
        self.assertEqual(fu.send_due(D0 + timedelta(hours=3)).sent, 0)  # one a day
        self.assertEqual(fu.send_due(D0 + timedelta(days=1)).sent, 0)  # step 2 is day 3
        self.assertEqual(fu.send_due(D0 + timedelta(days=3)).sent, 1)
        self.assertEqual(fu.send_due(D0 + timedelta(days=7)).sent, 1)
        self.assertEqual(fu.send_due(D0 + timedelta(days=12)).sent, 1)
        self.assertEqual(fu.send_due(D0 + timedelta(days=20)).sent, 0)
        self.assertEqual([m.subject for m in mail.outbox], [s["subject"] for s in STEPS])

    def test_links_are_tagged_and_site_links_carry_the_reader_key(self) -> None:
        self.enable()
        fu.send_due(D0)
        html = mail.outbox[-1].alternatives[0][0]
        self.assertIn("utm_campaign=guide-scams", html)
        self.assertIn(f"k={self.sub.access_key}", html)
        self.assertIn(self.sub.token, mail.outbox[-1].extra_headers["List-Unsubscribe"])

    def test_waits_a_day_after_a_series_email(self) -> None:
        self.enable()
        SeriesSend.objects.create(series_id=self._series_id(), subscriber=self.sub, article_id=self._episode_id(),
                                  status="sent", local_date=D0.date())
        result = fu.send_due(D0)
        self.assertEqual((result.sent, result.deferred), (0, 1))
        self.assertEqual(fu.send_due(D0 + timedelta(days=1)).sent, 1)

    def test_not_before_nine_local_and_not_after_unsubscribing(self) -> None:
        self.enable()
        self.assertEqual(fu.send_due(D0 - timedelta(hours=3)).sent, 0)  # 07:00 in India
        newsletter.unsubscribe(self.sub.token)
        self.assertEqual(fu.send_due(D0).sent, 0)

    def test_test_send_for_approval(self) -> None:
        r = self.client.post(f"{GUIDES}{self.guide.pk}/followups/test/")
        self.assertEqual(r.json()["sent"], 4)
        self.assertTrue(all(m.subject.startswith("[Test] ") for m in mail.outbox))
        self.assertNotIn("k=", mail.outbox[0].alternatives[0][0])
        self.assertEqual(GuideFollowupSend.objects.count(), 0)

    def test_staff_sees_steps_and_numbers(self) -> None:
        self.enable()
        fu.send_due(D0)
        body = self.client.get(f"{GUIDES}{self.guide.pk}/").json()
        self.assertTrue(body["followups_enabled"])
        self.assertEqual([s["sent"] for s in body["followup_stats"]], [1, 0, 0, 0])

    # -- helpers: a series episode for the "already emailed today" case
    def _series_id(self) -> int:
        return self._episode().series_id

    def _episode_id(self) -> int:
        return self._episode().pk

    def _episode(self):  # type: ignore[no-untyped-def]
        from .models import Article, Series

        if not hasattr(self, "_ep"):
            show = Series.objects.create(title="S", slug="s", status="published")
            self._ep = Article.objects.create(title="E", slug="e", series=show, episode=1, body_html="<p>x</p>")
        return self._ep
