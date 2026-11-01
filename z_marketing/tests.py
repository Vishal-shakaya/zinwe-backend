from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.mail import get_connection
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from z_blog.models import NewsletterSubscriber

from . import outreach
from .models import Audience, Campaign, Contact, ContactEvent, OutreachSend, Suppression

BOX = dict(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", EMAIL_OUTREACH_HOST="smtp.zoho.in",
    EMAIL_OUTREACH_HOST_USER="hello@zinwe.tech", EMAIL_OUTREACH_HOST_PASSWORD="x", ZINWE_OUTREACH_GAP_SECONDS=0,
)
Status = Contact.Status


class Base(TestCase):
    def setUp(self) -> None:
        patcher = mock.patch("z_notification.channels.email.outreach_connection", side_effect=lambda: get_connection())
        patcher.start()
        self.addCleanup(patcher.stop)
        background = mock.patch("z_marketing.outreach.start_background")
        background.start()
        self.addCleanup(background.stop)
        self.campaign = Campaign.objects.get(name="US CPA launch")
        self.staff = get_user_model().objects.create_user(email="staff@zinwe.io", password="x", is_staff=True)
        self.client_api = APIClient()
        self.client_api.force_authenticate(self.staff)

    def contact(self, email: str = "dana@mesaridge.example", **extra) -> Contact:  # type: ignore[no-untyped-def]
        fields = dict(first_name="Dana", firm="Mesa Ridge CPAs", city="Dallas", next_date=timezone.localdate())
        fields.update(extra)
        return Contact.objects.create(campaign=self.campaign, email=email, **fields)

    def ready(self) -> None:
        """Every launch check passed by hand."""
        Campaign.objects.filter(pk=self.campaign.pk).update(
            status=Campaign.Status.ACTIVE, rules_checked_at=timezone.now(), test_sent_at=timezone.now(),
            dns_result={"spf": True, "dkim": True, "dmarc": True},
        )
        self.campaign.refresh_from_db()

    def send_batch(self) -> int:
        approved = outreach.approve(self.campaign, len(outreach.due(self.campaign)), "staff@zinwe.io")
        outreach.send_pending()
        return approved


class SeedTests(TestCase):
    def test_one_audience_one_draft_campaign_twelve_emails_no_contacts(self) -> None:
        self.assertEqual(Audience.objects.count(), 1)
        campaign = Campaign.objects.get()
        self.assertEqual(campaign.status, Campaign.Status.DRAFT)
        self.assertEqual(campaign.emails.count(), 12)
        self.assertEqual(campaign.contacts.count(), 0)


class ImportTests(Base):
    def test_only_named_new_addresses_get_in(self) -> None:
        self.contact()
        Suppression.objects.create(email="ben@stonebridge.example", reason="Unsubscribed")
        NewsletterSubscriber.objects.create(email="left@letter.example", status=NewsletterSubscriber.Status.UNSUBSCRIBED)
        text = (
            "Ella, Prairie Wind CPAs, ella@prairiewind.example, Dallas\n"
            "Jon, Mesa Ridge CPAs, jon@mesaridge.example, Dallas\n"
            "Ben, Stonebridge CPAs, ben@stonebridge.example, Dallas\n"
            "Lee, Letter LLC, left@letter.example, Austin\n"
            "Ira, North Fork Tax, info@northfork.example, Houston\n"
            "broken line\n"
        )
        results = [r["message"] for r in outreach.import_contacts(self.campaign, text, "staff@zinwe.io")]
        self.assertEqual(results, [
            "Added", "Firm already in the list, skipped", "Blocked, skipped", "Blocked, skipped",
            "Shared inbox, find a named person", "Check the format",
        ])
        self.assertEqual(self.campaign.contacts.count(), 2)


class FlowTests(Base):
    def test_two_cold_emails_then_it_stops(self) -> None:
        c = self.contact()
        outreach.after_send(c, 1, {s.n: s for s in self.campaign.emails.all()})
        self.assertEqual((c.status, c.next_email, c.next_date), (Status.COLD, 2, timezone.localdate() + timedelta(days=4)))
        outreach.after_send(c, 2, {s.n: s for s in self.campaign.emails.all()})
        self.assertEqual((c.status, c.next_email, c.next_date), (Status.WAITING, 0, None))

    def test_yes_opens_the_setup_emails_and_keeps_the_proof(self) -> None:
        c = self.contact(status=Status.WAITING, last_email=2, next_email=0, next_date=None)
        outreach.act(c, "yes", "staff@zinwe.io", 'Reply: "yes please"')
        self.assertEqual((c.status, c.next_email), (Status.OPTED, 3))
        self.assertEqual(c.proof, 'Reply: "yes please"')

    def test_installed_skips_setup_emails(self) -> None:
        c = self.contact(status=Status.OPTED, last_email=2, next_email=3)
        outreach.act(c, "installed", "staff@zinwe.io")
        self.assertEqual(c.next_email, 5)

    def test_question_pauses_and_resume_restores(self) -> None:
        c = self.contact(status=Status.OPTED, last_email=5, next_email=6)
        outreach.act(c, "question", "staff@zinwe.io")
        self.assertEqual((c.status, c.next_email), (Status.PAUSED, 0))
        outreach.act(c, "resume", "staff@zinwe.io")
        self.assertEqual((c.status, c.next_email), (Status.OPTED, 6))

    def test_signed_up_without_a_yes_ends_after_email_5(self) -> None:
        c = self.contact(status=Status.SIGNED, last_email=4, next_email=5)
        outreach.after_send(c, 5, {s.n: s for s in self.campaign.emails.all()})
        self.assertEqual(c.status, Status.CLOSED)

    def test_no_blocks_for_good_in_every_campaign(self) -> None:
        c = self.contact()
        other = Campaign.objects.create(name="Second", audience=self.campaign.audience)
        twin = Contact.objects.create(campaign=other, first_name="D", firm="M", email=c.email, next_date=timezone.localdate())
        outreach.act(c, "no", "staff@zinwe.io")
        twin.refresh_from_db()
        self.assertEqual(twin.status, Status.SUPPRESSED)
        self.assertTrue(Suppression.objects.filter(email=c.email).exists())
        with self.assertRaises(outreach.OutreachError):
            outreach.act(c, "yes", "staff@zinwe.io")

    def test_a_spam_complaint_pauses_the_campaign(self) -> None:
        self.ready()
        c = self.contact()
        outreach.act(c, "complaint", "staff@zinwe.io")
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, Campaign.Status.PAUSED)
        with self.assertRaisesMessage(outreach.OutreachError, "marked your email as spam"):
            outreach.approve(self.campaign, 0, "staff@zinwe.io")


@override_settings(**BOX)
class SendTests(Base):
    def test_nothing_sends_without_launch_or_a_mailbox(self) -> None:
        self.contact()
        with self.assertRaises(outreach.OutreachError):
            outreach.approve(self.campaign, 1, "staff@zinwe.io")
        self.ready()
        with override_settings(EMAIL_OUTREACH_HOST=""), self.assertRaises(outreach.OutreachError):
            outreach.approve(self.campaign, 1, "staff@zinwe.io")
        self.assertEqual(OutreachSend.objects.count(), 0)

    def test_job_sends_only_what_a_person_approved(self) -> None:
        self.ready()
        self.contact()
        outreach.send_pending()
        self.assertEqual(mail.outbox, [])

    def test_email_is_personal_comes_from_outreach_and_can_be_unsubscribed(self) -> None:
        self.ready()
        c = self.contact()
        self.assertEqual(self.send_batch(), 1)
        m = mail.outbox[0]
        self.assertEqual(m.from_email, "Vishal Shakaya <hello@zinwe.tech>")
        self.assertEqual(m.reply_to, ["hello@zinwe.tech"])
        self.assertIn("Mesa Ridge CPAs", m.body)
        self.assertNotIn("{{", m.body + m.subject)
        self.assertIn(f"/api/marketing/unsubscribe/{c.token}/", m.extra_headers["List-Unsubscribe"])
        self.assertEqual(m.extra_headers["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        self.assertIn("SKYTECHNO", m.body)
        c.refresh_from_db()
        self.assertEqual((c.status, c.last_email, c.next_email), (Status.COLD, 1, 2))

    def test_daily_limit_holds_across_batches(self) -> None:
        self.ready()
        Campaign.objects.filter(pk=self.campaign.pk).update(daily_limit=2)
        self.campaign.refresh_from_db()
        for i in range(3):
            self.contact(f"p{i}@firm{i}.example")
        outreach.approve(self.campaign, 2, "staff@zinwe.io")
        outreach.send_pending()
        self.assertEqual(len(mail.outbox), 2)
        with self.assertRaisesMessage(outreach.OutreachError, "limit"):
            outreach.approve(self.campaign, 0, "staff@zinwe.io")

    def test_a_list_that_changed_is_refused(self) -> None:
        self.ready()
        self.contact()
        with self.assertRaisesMessage(outreach.OutreachError, "changed"):
            outreach.approve(self.campaign, 5, "staff@zinwe.io")

    def test_unsubscribing_before_its_turn_skips_the_email(self) -> None:
        self.ready()
        c = self.contact()
        outreach.approve(self.campaign, 1, "staff@zinwe.io")
        outreach.unsubscribe(c.token)
        outreach.send_pending()
        self.assertEqual(mail.outbox, [])
        self.assertEqual(OutreachSend.objects.get().status, OutreachSend.Status.SKIPPED)

    def test_pausing_stops_the_rest_of_the_batch(self) -> None:
        self.ready()
        self.contact()
        outreach.approve(self.campaign, 1, "staff@zinwe.io")
        outreach.pause(self.campaign, "x")
        outreach.send_pending()
        self.assertEqual(mail.outbox, [])

    def test_a_crash_mid_send_is_never_resent_alone_but_can_be_approved_again(self) -> None:
        self.ready()
        c = self.contact()
        OutreachSend.objects.create(
            contact=c, n=1, status=OutreachSend.Status.SENDING, approved_by="s@zinwe.io",
            claimed_at=timezone.now() - timedelta(hours=1),
        )
        self.assertEqual(outreach.due(self.campaign), [])
        self.assertEqual(outreach.send_pending().stuck, 1)
        self.assertEqual(mail.outbox, [])
        self.assertEqual(self.send_batch(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_a_failed_send_keeps_the_contact_due(self) -> None:
        self.ready()
        c = self.contact()
        outreach.approve(self.campaign, 1, "staff@zinwe.io")
        with mock.patch("z_notification.channels.email.outreach_connection", side_effect=RuntimeError("down")):
            result = outreach.send_pending()
        self.assertEqual(result.failed, 1)
        c.refresh_from_db()
        self.assertEqual((c.status, c.next_email), (Status.NEW, 1))
        self.assertEqual(outreach.due(self.campaign), [c])

    def test_signed_only_line_goes_only_to_signed_up_contacts(self) -> None:
        step = self.campaign.emails.get(n=5)
        signed = outreach._body(step, self.contact(status=Status.SIGNED))
        opted = outreach._body(step, self.contact("b@b.example", status=Status.OPTED))
        self.assertIn('reply "yes"', signed)
        self.assertNotIn("[", signed)
        self.assertNotIn("remaining tips", opted)
        self.assertNotIn("[", opted)

    def test_test_email_marks_the_check_and_touches_no_contact(self) -> None:
        outreach.send_test(self.campaign, 1, "staff@zinwe.io")
        self.assertTrue(mail.outbox[0].subject.startswith("[Test] "))
        self.campaign.refresh_from_db()
        self.assertIsNotNone(self.campaign.test_sent_at)
        self.assertEqual(Contact.objects.count(), 0)


@override_settings(**BOX)
class LaunchTests(Base):
    def test_launch_needs_every_check(self) -> None:
        with self.assertRaisesMessage(outreach.OutreachError, "Finish these first"):
            outreach.launch(self.campaign, "staff@zinwe.io")
        self.campaign.rules_checked_at = self.campaign.test_sent_at = timezone.now()
        self.campaign.dns_result = {"spf": True, "dkim": True, "dmarc": True}
        self.campaign.save()
        self.contact()
        self.assertEqual(outreach.launch(self.campaign, "staff@zinwe.io").status, Campaign.Status.ACTIVE)

    def test_a_zinwe_io_sender_is_refused(self) -> None:
        with override_settings(ZINWE_OUTREACH_FROM="V <v@mail.zinwe.io>"):
            self.assertFalse(outreach.separate_domain())

    def test_dns_check_reads_public_records(self) -> None:
        records = {
            "zinwe.tech": ["v=spf1 include:zoho.in ~all"], "zmail._domainkey.zinwe.tech": ["v=DKIM1; k=rsa; p=x"],
            "_dmarc.zinwe.tech": ["v=DMARC1; p=none"],
        }
        with mock.patch("z_marketing.outreach._txt", side_effect=lambda n: records[n]):
            self.assertEqual(outreach.check_dns(self.campaign), {"spf": True, "dkim": True, "dmarc": True})
        records["zinwe.tech"] = ["v=spf1 include:_spf.reach.hostinger.com ~all"]
        with mock.patch("z_marketing.outreach._txt", side_effect=lambda n: records[n]):
            self.assertFalse(outreach.check_dns(self.campaign)["spf"])


class ApiTests(Base):
    def test_staff_only(self) -> None:
        self.assertEqual(APIClient().get("/api/admin/marketing/campaigns/").status_code, 401)

    def test_audience_needs_segment_problem_channel(self) -> None:
        r = self.client_api.post("/api/admin/marketing/audiences/", {"name": "X", "segments": ["A"], "problems": ["B"]}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("channels", r.json())
        r = self.client_api.post(
            "/api/admin/marketing/audiences/",
            {"name": "X", "segments": ["A"], "problems": ["B"], "channels": ["Email"], "confidence": "Heard once"}, format="json",
        )
        self.assertEqual(r.status_code, 201)

    def test_campaign_detail_and_edit_email(self) -> None:
        pk = self.campaign.pk
        data = self.client_api.get(f"/api/admin/marketing/campaigns/{pk}/").json()
        self.assertEqual(len(data["emails"]), 12)
        self.assertEqual(data["status"], "draft")
        self.client_api.put(f"/api/admin/marketing/campaigns/{pk}/emails/1/", {"subject": "New"}, format="json")
        self.assertEqual(self.campaign.emails.get(n=1).subject, "New")

    def test_import_and_act_over_http(self) -> None:
        pk = self.campaign.pk
        r = self.client_api.post(f"/api/admin/marketing/campaigns/{pk}/contacts/import/", {"text": "Ella, Prairie CPAs, ella@prairie.example, Dallas"}, format="json")
        self.assertEqual(r.json()["results"][0]["result"], "good")
        c = Contact.objects.get()
        r = self.client_api.post(f"/api/admin/marketing/contacts/{c.pk}/act/", {"action": "question"}, format="json")
        self.assertEqual(r.json()["status"], "paused")
        self.assertEqual(self.client_api.get(f"/api/admin/marketing/campaigns/{pk}/contacts/").json()[0]["stage"], "On hold")

    def test_launch_refusal_reaches_staff(self) -> None:
        r = self.client_api.post(f"/api/admin/marketing/campaigns/{self.campaign.pk}/launch/", {}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Finish these first", r.json()["detail"])


class UnsubscribeTests(Base):
    def test_opening_the_link_changes_nothing_but_the_button_does(self) -> None:
        c = self.contact()
        url = f"/api/marketing/unsubscribe/{c.token}/"
        self.assertContains(self.client.get(url), "Unsubscribe")
        c.refresh_from_db()
        self.assertNotEqual(c.status, Status.SUPPRESSED)
        self.assertEqual(self.client.post(url).status_code, 200)
        c.refresh_from_db()
        self.assertEqual(c.status, Status.SUPPRESSED)
        self.assertTrue(Suppression.objects.filter(email=c.email, reason="Unsubscribed").exists())
        self.assertEqual(self.client.post(url).status_code, 200)  # the mail client may retry
        self.assertEqual(ContactEvent.objects.filter(contact=c, text="Unsubscribed").count(), 1)

    def test_unknown_token_gives_nothing_away(self) -> None:
        self.assertEqual(self.client.post("/api/marketing/unsubscribe/nope/").status_code, 200)
