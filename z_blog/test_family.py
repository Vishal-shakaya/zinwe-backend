"""SAFE BY DESIGN (Series 03): the family episode email and its 'share with your child' Family Card block."""
from __future__ import annotations

from datetime import timedelta
from urllib.parse import unquote

from django.core import mail
from django.test import override_settings
from django.utils import timezone

from . import drip
from .models import NewsletterSubscriber, Series
from .series_brand import brand, code_of
from .test_series import EMAIL, SeriesTestCase
from .tests import STAFF

Sub = NewsletterSubscriber.Status
BODY = (
    "<p>" + "word " * 300 + "</p>"
    "<h2>Try this together: Check who can install things</h2><ol><li>One</li></ol>"
    "<h2>For your child: Free stuff? Pause first.</h2>"
    '<p><img src="/api/blog/images/00000000-0000-0000-0000-000000000001/" alt="Family Card 01: Free stuff? Pause first"></p>'
)


@EMAIL
@override_settings(ZINWE_SERIES_WELCOME_INLINE=True, ZINWE_APP_URL="https://zinwe.test")
class FamilySeriesTests(SeriesTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sbd = self.make_series(
            drip_enabled=True, slug="safe-by-design", title="Safe by Design", position=2, drip_weekdays="5",
        )
        Series.objects.filter(pk=self.sbd["id"]).update(
            seasons=[{"number": 1, "title": "The First Click", "theme": ""}], tagline="Protect their world.")
        a = self.make_episode(
            self.sbd["id"], publish=False, title="The Free Game That Wasn't Free", newsletter_subject="He brought me the tablet",
            body_html=BODY, tags="safe by design, ages 7-10",
        )
        self.client.post(f"{STAFF}{a['id']}/publish/", {"at": (timezone.now() - timedelta(days=1)).isoformat()}, format="json")

    def send(self):  # type: ignore[no-untyped-def]
        sub = NewsletterSubscriber.objects.create(
            email="parent@example.com", status=Sub.ACTIVE, confirmed_at=timezone.now(), source="/blog/series/safe-by-design",
        )
        self.assertEqual(drip.welcome(sub), 1)
        return mail.outbox[0]

    def test_brand(self) -> None:
        show = Series.objects.get(slug="safe-by-design")
        self.assertEqual(code_of(show), "sbd")
        self.assertEqual(brand(show).template, "newsletter_episode_family")
        self.assertTrue(brand(show).family_cards)

    def test_family_email(self) -> None:
        msg = self.send()
        html = msg.alternatives[0][0]
        self.assertEqual(msg.subject, "He brought me the tablet")
        self.assertIn("utm_campaign=sbd", msg.body)
        self.assertIn("Read the rest of the episode", html)
        self.assertIn("Check who can install things", html)  # Try this together
        self.assertIn("For parents · activity suits ages 7–10", html)
        self.assertIn("Season 1 · The First Click", html)
        self.assertIn("Zinwe · <span", html)
        # The Family Card block: picture, rule and share links to the public card page.
        self.assertIn("Free stuff? Pause first.", html)
        self.assertIn('src="https://zinwe.test/api/blog/images/00000000-0000-0000-0000-000000000001/"', html)
        self.assertIn('href="https://zinwe.test/family/01"', html)
        wa = html.split('href="https://wa.me/?text=')[1].split('"')[0]
        self.assertIn("https://zinwe.test/family/01", unquote(wa))
        self.assertIn("A little card for you from me: Free stuff? Pause first.", unquote(wa))
        self.assertIn('href="mailto:?subject=', html)
        self.assertIn("Family Card 01, to send on or print", msg.body)
        # The reader's own key never goes into what the parent forwards to a child.
        self.assertNotIn("?k=", unquote(wa))

    def test_other_series_have_no_family_block(self) -> None:
        iua = self.make_series(drip_enabled=True, slug="identity-under-attack", title="Identity Under Attack", position=1)
        self.assertFalse(brand(Series.objects.get(pk=iua["id"])).family_cards)
