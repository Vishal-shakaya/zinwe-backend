"""A second series: its own campaign code, its own episode email, one welcome email per new member."""
from __future__ import annotations

from datetime import timedelta

from django.core import mail
from django.test import override_settings
from django.utils import timezone

from z_auth.models import User

from . import drip
from .models import Article, NewsletterSubscriber, Series
from .series_brand import brand, code_of
from .test_series import EMAIL, SeriesTestCase
from .tests import STAFF

Sub = NewsletterSubscriber.Status


@EMAIL
@override_settings(ZINWE_SERIES_WELCOME_INLINE=True)
class SecondSeriesTests(SeriesTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tlf = self.make_series(drip_enabled=True, slug="the-last-file", position=0)
        self.iua = self.make_series(
            drip_enabled=True, slug="identity-under-attack", title="Identity Under Attack", position=1,
            drip_weekdays="2",
        )
        published = timezone.now() - timedelta(days=3)
        for show, subject in ((self.tlf, "TLF one"), (self.iua, "IUA one")):
            a = self.make_episode(
                show["id"], publish=False, title=f"{subject} title", newsletter_subject=subject,
                body_html="<p>" + "word " * 300 + "</p><h2>Do this once: Give three people a verified line</h2><p>x</p>",
            )
            self.client.post(f"{STAFF}{a['id']}/publish/", {"at": published.isoformat()}, format="json")

    def test_codes(self) -> None:
        self.assertEqual(code_of(Series.objects.get(slug="the-last-file")), "tlf")
        self.assertEqual(code_of(Series.objects.get(slug="identity-under-attack")), "iua")
        self.assertEqual(brand(Series(slug="the-quiet-office")).code, "tqo")

    def test_welcome_sends_only_the_series_they_signed_up_on(self) -> None:
        sub = NewsletterSubscriber.objects.create(
            email="new@example.com", status=Sub.ACTIVE, confirmed_at=timezone.now(),
            source="/blog/series/identity-under-attack/the-message-from-nina",
        )
        self.assertEqual(drip.welcome(sub), 1)
        self.assertEqual([m.subject for m in mail.outbox], ["IUA one"])

    def test_welcome_defaults_to_the_first_series(self) -> None:
        sub = NewsletterSubscriber.objects.create(email="blog@example.com", status=Sub.ACTIVE, confirmed_at=timezone.now(), source="/blog")
        self.assertEqual(drip.welcome(sub), 1)
        self.assertEqual([m.subject for m in mail.outbox], ["TLF one"])

    def test_identity_email_uses_its_own_look_and_campaign(self) -> None:
        sub = NewsletterSubscriber.objects.create(email="r@example.com", status=Sub.ACTIVE, confirmed_at=timezone.now(), source="/blog/series/identity-under-attack")
        drip.welcome(sub)
        msg = mail.outbox[0]
        html = msg.alternatives[0][0]
        self.assertIn("utm_campaign=iua", msg.body)
        self.assertNotIn("utm_campaign=tlf", msg.body)
        self.assertIn("READ THE FULL EPISODE", html)
        self.assertIn("Give three people a verified line", html)
        self.assertNotIn("CASE FILE", html)

    def test_last_file_email_is_unchanged(self) -> None:
        sub = NewsletterSubscriber.objects.create(email="t@example.com", status=Sub.ACTIVE, confirmed_at=timezone.now(), source="/blog")
        drip.welcome(sub)
        self.assertIn("utm_campaign=tlf", mail.outbox[0].body)
        self.assertIn("READ THE FULL CASE FILE", mail.outbox[0].alternatives[0][0])

    def test_stats_count_signups_per_series(self) -> None:
        User.objects.create_user(email="a@example.com", password="x-long-password-1", signup_source="iua:ep01:email")
        User.objects.create_user(email="b@example.com", password="x-long-password-2", signup_source="tlf:ep01:email")
        show = Series.objects.get(slug="identity-under-attack")
        r = self.client.get(f"/api/admin/blog/series/{show.pk}/stats/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["signups"], 1)
        self.assertEqual(Article.objects.filter(series=show).count(), 1)
