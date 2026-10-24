"""FIX-009: a sender that dies mid-send leaves its claim row in "sending". The hourly jobs take back claims older
than `claims.STALE_AFTER` and send them exactly once more -- or, when the email had already gone out, just record it."""
from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.core import mail
from django.test import override_settings
from django.utils import timezone

from . import drip, guide_followups as fu, newsletter
from .models import Article, Guide, GuideDownload, GuideFollowupSend, NewsletterDelivery, NewsletterIssue, SeriesSend
from .test_drip import MONDAY_IST_10
from .test_guide_followups import D0, STEPS
from .test_guides import GuideTestCase
from .test_series import EMAIL, SeriesTestCase
from .tests import STAFF


class Crash(BaseException):
    """The process dying (worker killed, OOM, deploy restart): nothing catches it."""


def crash(*args, **kwargs):  # type: ignore[no-untyped-def]
    raise Crash()


def crash_on_update(model):  # type: ignore[no-untyped-def]
    """Die on the save that records the result (after the email went out); creating the claim still works."""
    real = model.save

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs.get("update_fields"):
            raise Crash()
        return real(self, *args, **kwargs)

    return mock.patch.object(model, "save", save)


def wall_clock(hours: float):  # type: ignore[no-untyped-def]
    """The real clock, `hours` from now (claims are aged by the wall clock, not the scheduling `now`)."""
    return mock.patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(hours=hours))


@EMAIL
class NewsletterCrashTests(SeriesTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.member("a@example.com")
        self.member("b@example.com")
        a = self.create(title="Phishing in 2026", type="newsletter")
        self.client.post(f"{STAFF}{a['id']}/publish/", {}, format="json")
        self.issue = newsletter.queue(Article.objects.get(pk=a["id"]), "Phishing in 2026", self.staff)
        mail.outbox.clear()

    def test_a_claim_left_by_a_crash_is_sent_once_and_the_issue_finishes(self) -> None:
        real = newsletter._send
        calls = {"n": 0}

        def second_crashes(*args, **kwargs):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            if calls["n"] == 2:
                raise Crash()
            return real(*args, **kwargs)

        with mock.patch.object(newsletter, "_send", second_crashes), self.assertRaises(Crash):
            newsletter.send_pending()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(NewsletterDelivery.objects.filter(status="sending").count(), 1)

        newsletter.send_pending()  # straight after: the claim may still be in flight elsewhere, leave it
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(NewsletterIssue.objects.get().status, "sending")

        with wall_clock(1):
            self.assertEqual(newsletter.send_pending().sent, 1)
            self.assertEqual(newsletter.send_pending().sent, 0)
        self.assertEqual(sorted(m.to[0] for m in mail.outbox), ["a@example.com", "b@example.com"])
        self.assertEqual(NewsletterIssue.objects.get().status, "sent")
        self.assertFalse(NewsletterDelivery.objects.exclude(status="sent").exists())

    def test_a_crash_after_the_email_went_out_is_recorded_not_resent(self) -> None:
        with crash_on_update(NewsletterDelivery), self.assertRaises(Crash):
            newsletter.send_pending()
        self.assertEqual(len(mail.outbox), 1)  # delivered; the row never heard
        with wall_clock(1):
            newsletter.send_pending()
        self.assertEqual(len(mail.outbox), 2)  # the other subscriber only
        self.assertEqual(sorted(m.to[0] for m in mail.outbox), ["a@example.com", "b@example.com"])
        self.assertFalse(NewsletterDelivery.objects.exclude(status="sent").exists())
        self.assertEqual(NewsletterIssue.objects.get().status, "sent")


@EMAIL
@override_settings(ZINWE_SERIES_WELCOME_INLINE=True)
class DripCrashTests(SeriesTestCase):
    def setUp(self) -> None:
        super().setUp()
        show = self.make_series(drip_enabled=True)
        published = timezone.now() - timedelta(days=30)
        for n in range(1, 4):
            a = self.make_episode(show["id"], publish=False, title=f"Episode number {n}", newsletter_subject=f"Subject {n}")
            self.client.post(f"{STAFF}{a['id']}/publish/", {"at": (published + timedelta(minutes=n)).isoformat()}, format="json")
        from .models import NewsletterSubscriber

        NewsletterSubscriber.objects.create(email="reader@example.com", status="active", confirmed_at=timezone.now(), timezone="Asia/Kolkata")
        mail.outbox.clear()

    def subjects(self) -> list[str]:
        return [m.subject for m in mail.outbox]

    def test_a_stuck_episode_is_sent_again_not_skipped(self) -> None:
        with mock.patch.object(newsletter, "_send", crash), self.assertRaises(Crash):
            drip.send_due(MONDAY_IST_10)
        self.assertEqual(SeriesSend.objects.get().status, "sending")
        with wall_clock(2):
            drip.send_due(MONDAY_IST_10 + timedelta(hours=2))  # Monday: that day's email was tried already
            self.assertEqual(SeriesSend.objects.get().status, "failed")
            drip.send_due(MONDAY_IST_10 + timedelta(days=1))  # Tuesday: Episode 1 again, once
            drip.send_due(MONDAY_IST_10 + timedelta(days=1, hours=1))
            drip.send_due(MONDAY_IST_10 + timedelta(days=3))  # Thursday: Episode 2
        self.assertEqual(self.subjects(), ["Subject 1", "Subject 2"])

    def test_a_crash_after_the_episode_went_out_is_recorded_not_resent(self) -> None:
        with crash_on_update(SeriesSend), self.assertRaises(Crash):
            drip.send_due(MONDAY_IST_10)
        self.assertEqual(self.subjects(), ["Subject 1"])
        with wall_clock(2):
            drip.send_due(MONDAY_IST_10 + timedelta(days=1))
        self.assertEqual(self.subjects(), ["Subject 1", "Subject 2"])
        self.assertEqual(SeriesSend.objects.filter(status="sent").count(), 2)


@EMAIL
class GuideFollowupCrashTests(GuideTestCase):
    def setUp(self) -> None:
        super().setUp()
        g = self.make_guide(followups=STEPS)
        self.guide = Guide.objects.get(pk=g["id"])
        self.sub = self.member()
        self.sub.timezone = "Asia/Kolkata"
        self.sub.save()
        dl = GuideDownload.objects.create(guide=self.guide, subscriber=self.sub)
        GuideDownload.objects.filter(pk=dl.pk).update(first_at=D0 - timedelta(hours=1))
        self.client.patch(f"/api/admin/blog/guides/{self.guide.pk}/", {"followups_enabled": True}, format="json")
        Guide.objects.filter(pk=self.guide.pk).update(followups_enabled_at=D0 - timedelta(days=1))
        mail.outbox.clear()

    def test_a_stuck_follow_up_is_sent_again_once(self) -> None:
        with mock.patch.object(fu, "_send", crash), self.assertRaises(Crash):
            fu.send_due(D0)
        self.assertEqual(GuideFollowupSend.objects.get().status, "sending")
        with wall_clock(2):
            fu.send_due(D0 + timedelta(hours=2))
            fu.send_due(D0 + timedelta(days=1))
            fu.send_due(D0 + timedelta(days=1, hours=1))
        self.assertEqual([m.subject for m in mail.outbox], ["Your guide, and where to start"])
        self.assertEqual(GuideFollowupSend.objects.get().status, "sent")
