"""Internal admin API (M-ADMIN): staff-only guard, subscribers + actions, payment queue, feedback, metrics."""
from __future__ import annotations

from datetime import date, timedelta
from unittest import mock
from decimal import Decimal

from django.core import mail
from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from z_auth.models import (
    Company,
    CustomerRemark,
    Feedback,
    ManualPayment,
    Plan,
    PlanActivation,
    Subscription,
    User,
)
from z_notification.models import Notification

ME = "/api/admin/me/"
METRICS = "/api/admin/metrics/"
PAYMENTS = "/api/admin/payments/"
FEEDBACK = "/api/admin/feedback/"
SUBS = "/api/admin/subscribers/"


class AdminApiTestCase(APITestCase):
    def setUp(self) -> None:
        self.staff = User.objects.create_superuser("ops@zinwe.local", "ops-12345678")
        self.as_staff()

    def as_staff(self) -> None:
        token, _ = Token.objects.get_or_create(user=self.staff)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

    def as_user(self, user: User) -> None:
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

    def make_owner(self, email: str = "owner@acme.in", **extra) -> User:  # type: ignore[no-untyped-def]
        return User.objects.create_user(email, "pw-12345678", email_verified_at=timezone.now(), **extra)


class GuardTests(AdminApiTestCase):
    def test_staff_can_reach_me(self) -> None:
        response = self.client.get(ME)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.json()["is_staff"])

    def test_non_staff_is_forbidden(self) -> None:
        self.as_user(self.make_owner())
        self.assertEqual(self.client.get(METRICS).status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_is_unauthorized(self) -> None:
        self.client.credentials()
        self.assertEqual(self.client.get(SUBS).status_code, status.HTTP_401_UNAUTHORIZED)


class SubscriberFixture(AdminApiTestCase):
    """An owner with a company, and a helper for a payment waiting to be checked."""

    def setUp(self) -> None:
        super().setUp()
        self.owner = self.make_owner()
        self.company = Company.objects.create(
            owner=self.owner, name="Acme", industry="retail", team_size="11-25",
            phone="+91 90000 00000", city="Delhi", state="Delhi",
        )
        self.paid = Plan.objects.get(code=Plan.STARTUPS)

    def _pending_with_payment(self) -> ManualPayment:
        sub = Subscription.objects.create(
            owner=self.owner, company=self.company, plan=self.paid,
            status=Subscription.Status.PENDING_PAYMENT, amount_inr=self.paid.price_inr_monthly,
        )
        return ManualPayment.objects.create(
            subscription=sub, method="upi", amount_inr=self.paid.price_inr_monthly,
            reference="UTR1", paid_on=date.today(), payer_name="Asha",
            status=ManualPayment.Status.SUBMITTED,
        )


class SubscriberTests(SubscriberFixture):
    def test_list_shows_owner_not_staff(self) -> None:
        body = self.client.get(SUBS).json()
        emails = [row["email"] for row in body]
        self.assertIn(self.owner.email, emails)
        self.assertNotIn(self.staff.email, emails)

    def test_approve_payment_activates_subscription(self) -> None:
        self._pending_with_payment()
        response = self.client.post(f"{SUBS}{self.owner.id}/approve-payment/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["subscription"]["status"], "active")
        self.assertEqual(response.json()["payment"]["status"], "verified")

    def test_approve_without_payment_is_400(self) -> None:
        response = self.client.post(f"{SUBS}{self.owner.id}/approve-payment/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reject_payment(self) -> None:
        self._pending_with_payment()
        response = self.client.post(
            f"{SUBS}{self.owner.id}/reject-payment/", {"reason": "Amount mismatch"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["payment"]["status"], "rejected")

    def test_block_and_unblock(self) -> None:
        self.client.post(f"{SUBS}{self.owner.id}/block/")
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.is_active)
        self.client.post(f"{SUBS}{self.owner.id}/unblock/")
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_active)

    def test_cannot_block_staff(self) -> None:
        # The <id> must be a non-staff owner; a staff id 404s.
        response = self.client.post(f"{SUBS}{self.staff.id}/block/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_change_plan_grants_immediately(self) -> None:
        free = Plan.objects.get(code=Plan.FREE)
        response = self.client.post(
            f"{SUBS}{self.owner.id}/change-plan/", {"plan": free.code}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["plan"]["code"], "free")
        self.assertEqual(response.json()["subscription"]["status"], "active")
        self.assertTrue(
            PlanActivation.objects.filter(user=self.owner, plan=free, is_active=True).exists()
        )

    def test_change_plan_unknown_is_400(self) -> None:
        response = self.client.post(
            f"{SUBS}{self.owner.id}/change-plan/", {"plan": "ghost"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class MetricsTests(AdminApiTestCase):
    def test_metrics_shape(self) -> None:
        self.make_owner("a@acme.in")
        body = self.client.get(METRICS).json()
        self.assertIn("totals", body)
        self.assertIn("revenue_inr", body["totals"])
        self.assertNotIn("early_access", body["totals"])
        self.assertEqual(body["totals"]["feedback"], {"count": 0, "average": None})
        self.assertEqual(len(body["signups"]), 30)
        self.assertEqual(len(body["revenue"]), 6)
        self.assertGreaterEqual(body["totals"]["users"], 1)


class EarlyAccessRemovedTests(AdminApiTestCase):
    def test_admin_early_access_endpoints_are_gone(self) -> None:
        self.assertEqual(self.client.get("/api/admin/early-access/").status_code, status.HTTP_404_NOT_FOUND)


class SubscriberAccountTests(SubscriberFixture):
    """Password, delete, and the payment history / billing cycles on each row."""

    def _pay(self, status_: str, *, days_ago: int, amount: str = "999.00") -> ManualPayment:
        when = timezone.now() - timedelta(days=days_ago)
        sub = Subscription.objects.create(
            owner=self.owner, company=self.company, plan=self.paid,
            status=Subscription.Status.EXPIRED if status_ == "verified" else Subscription.Status.PENDING_PAYMENT,
            amount_inr=self.paid.price_inr_monthly, created_at=when,
        )
        return ManualPayment.objects.create(
            subscription=sub, method="upi", amount_inr=Decimal(amount), reference=f"UTR{days_ago}",
            paid_on=when.date(), payer_name="Asha", status=status_, submitted_at=when,
            reviewed_at=when if status_ != "submitted" else None,
        )

    def row(self) -> dict:
        return next(r for r in self.client.get(SUBS).json() if r["id"] == self.owner.id)

    def test_second_payment_is_cycle_two(self) -> None:
        first = self._pay("verified", days_ago=60)
        rejected = self._pay("rejected", days_ago=31)
        second = self._pay("verified", days_ago=30)
        waiting = self._pay("submitted", days_ago=0)
        row = self.row()
        self.assertEqual(row["billing"], {"cycles_paid": 2, "total_paid_inr": "1998.00"})
        cycles = {p["id"]: p["cycle"] for p in row["payments"]}
        self.assertEqual(cycles, {first.id: 1, rejected.id: None, second.id: 2, waiting.id: 3})
        self.assertEqual(row["payments"][0]["id"], waiting.id)  # newest first
        self.assertEqual(row["payment"]["cycle"], 3)  # starts cycle 3 once approved

    def test_team_members_listed(self) -> None:
        from z_devices.models import Member

        Member.objects.create(owner=self.owner, name="Ravi", email="ravi@acme.in")
        Member.objects.create(owner=self.owner, name="Asha", email="asha@acme.in")
        other = self.make_owner("other@acme.in")
        Member.objects.create(owner=other, name="Not theirs", email="x@other.in")
        row = self.row()
        self.assertEqual([(m["name"], m["email"]) for m in row["team"]["members"]],
                         [("Asha", "asha@acme.in"), ("Ravi", "ravi@acme.in")])
        self.assertEqual((row["team"]["members"][0]["devices"], row["team"]["devices"]), (0, 0))
        self.assertEqual(len(self.client.get(f"{SUBS}{self.owner.id}/").json()["team"]["members"]), 2)

    def test_no_payments_means_zero_cycles(self) -> None:
        row = self.row()
        self.assertEqual(row["billing"], {"cycles_paid": 0, "total_paid_inr": "0"})
        self.assertEqual(row["payments"], [])

    def test_set_password(self) -> None:
        Token.objects.create(user=self.owner)
        response = self.client.post(
            f"{SUBS}{self.owner.id}/set-password/", {"password": "Fresh-Passphrase-42"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password("Fresh-Passphrase-42"))
        self.assertFalse(Token.objects.filter(user=self.owner).exists())  # signed out everywhere
        self.assertNotIn("password", response.json())  # never echoed back

    def test_weak_password_is_refused(self) -> None:
        response = self.client.post(f"{SUBS}{self.owner.id}/set-password/", {"password": "123"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("too short", response.json()["detail"])
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password("pw-12345678"))

    def test_block_signs_the_customer_out(self) -> None:
        Token.objects.create(user=self.owner)
        self.client.post(f"{SUBS}{self.owner.id}/block/")
        self.assertFalse(Token.objects.filter(user=self.owner).exists())

    def test_delete_customer(self) -> None:
        self._pending_with_payment()
        Feedback.objects.create(owner=self.owner, rating=7)
        response = self.client.delete(f"{SUBS}{self.owner.id}/")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(User.objects.filter(pk=self.owner.id).exists())
        self.assertFalse(Company.objects.filter(pk=self.company.pk).exists())
        self.assertFalse(ManualPayment.objects.exists())
        self.assertFalse(Feedback.objects.exists())

    def test_cannot_delete_staff(self) -> None:
        self.assertEqual(self.client.delete(f"{SUBS}{self.staff.id}/").status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(User.objects.filter(pk=self.staff.id).exists())


class PaymentQueueTests(SubscriberFixture):
    """Plan activation: a submitted payment shows its details; the plan activates only on approval."""

    def test_queue_lists_submitted_payment_with_details(self) -> None:
        pay = self._pending_with_payment()
        body = self.client.get(PAYMENTS, {"status": "submitted"}).json()
        self.assertEqual(len(body), 1)
        row = body[0]
        self.assertEqual(row["id"], pay.id)
        picked = {k: row[k] for k in ("method", "amount_inr", "expected_inr", "reference", "payer_name", "status", "cycle")}
        self.assertEqual(
            picked,
            {"method": "upi", "amount_inr": "999.00", "expected_inr": "999.00", "reference": "UTR1",
             "payer_name": "Asha", "status": "submitted", "cycle": 1},
        )
        self.assertEqual(row["customer"]["email"], self.owner.email)
        self.assertEqual(row["customer"]["company"], "Acme")
        self.assertEqual(row["plan"]["code"], "startups")
        self.assertFalse(PlanActivation.objects.filter(user=self.owner, is_active=True).exists())

    def test_filter_and_bad_status(self) -> None:
        self._pending_with_payment()
        self.assertEqual(self.client.get(PAYMENTS, {"status": "verified"}).json(), [])
        self.assertEqual(len(self.client.get(PAYMENTS).json()), 1)
        self.assertEqual(self.client.get(PAYMENTS, {"status": "nope"}).status_code, status.HTTP_400_BAD_REQUEST)

    def test_approve_activates_plan(self) -> None:
        pay = self._pending_with_payment()
        response = self.client.post(f"{PAYMENTS}{pay.id}/approve/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        body = response.json()
        self.assertEqual((body["status"], body["subscription_status"]), ("verified", "active"))
        self.assertEqual(body["reviewed_by"], self.staff.email)
        self.assertTrue(PlanActivation.objects.filter(user=self.owner, plan=self.paid, is_active=True).exists())
        # Approving twice is refused.
        self.assertEqual(self.client.post(f"{PAYMENTS}{pay.id}/approve/").status_code, status.HTTP_400_BAD_REQUEST)

    def test_reject_needs_reason_and_keeps_plan_off(self) -> None:
        pay = self._pending_with_payment()
        no_reason = self.client.post(f"{PAYMENTS}{pay.id}/reject/", {}, format="json")
        self.assertEqual(no_reason.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.client.post(f"{PAYMENTS}{pay.id}/reject/", {"reason": "UTR not found"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual((body["status"], body["rejection_reason"], body["cycle"]), ("rejected", "UTR not found", None))
        self.assertFalse(PlanActivation.objects.filter(user=self.owner, is_active=True).exists())

    def test_unknown_payment_is_404(self) -> None:
        self.assertEqual(self.client.post(f"{PAYMENTS}999/approve/").status_code, status.HTTP_404_NOT_FOUND)

    def test_owner_cannot_use_queue(self) -> None:
        self.as_user(self.owner)
        self.assertEqual(self.client.get(PAYMENTS).status_code, status.HTTP_403_FORBIDDEN)


class FeedbackAdminTests(AdminApiTestCase):
    def test_list_and_summary(self) -> None:
        owner = self.make_owner("asha@acme.in", full_name="Asha")
        Company.objects.create(owner=owner, name="Acme", industry="retail", team_size="11-25",
                               phone="+91 90000 00000", city="Delhi", state="Delhi")
        Feedback.objects.create(owner=owner, rating=9, message="Love the USB alerts")
        Feedback.objects.create(owner=owner, rating=6, message="")
        body = self.client.get(FEEDBACK).json()
        self.assertEqual((body["summary"]["count"], body["summary"]["average"]), (2, 7.5))
        self.assertEqual(len(body["summary"]["distribution"]), 10)
        self.assertEqual(body["summary"]["distribution"][8], {"rating": 9, "count": 1})
        self.assertEqual(body["rows"][0]["rating"], 6)  # newest first
        self.assertEqual(
            body["rows"][1]["customer"],
            {"id": owner.id, "email": "asha@acme.in", "full_name": "Asha", "company": "Acme"},
        )
        self.assertEqual(self.client.get(METRICS).json()["totals"]["feedback"], {"count": 2, "average": 7.5})

    def test_empty(self) -> None:
        body = self.client.get(FEEDBACK).json()
        self.assertEqual((body["summary"]["count"], body["summary"]["average"], body["rows"]), (0, None, []))


class RemarkTests(SubscriberFixture):
    def url(self) -> str:
        return f"{SUBS}{self.owner.id}/remark/"

    def test_save_update_and_clear(self) -> None:
        body = self.client.post(self.url(), {"text": "  Called on 11 Oct, wants a demo  "}, format="json").json()
        self.assertEqual(body["remark"]["text"], "Called on 11 Oct, wants a demo")
        self.assertEqual(body["remark"]["updated_by"], self.staff.email)
        body = self.client.post(self.url(), {"text": "Demo booked"}, format="json").json()
        self.assertEqual(body["remark"]["text"], "Demo booked")
        self.assertEqual(CustomerRemark.objects.count(), 1)
        listed = next(r for r in self.client.get(SUBS).json() if r["id"] == self.owner.id)
        self.assertEqual(listed["remark"]["text"], "Demo booked")
        body = self.client.post(self.url(), {"text": ""}, format="json").json()
        self.assertIsNone(body["remark"])
        self.assertFalse(CustomerRemark.objects.exists())

    def test_never_reaches_the_customer(self) -> None:
        self.client.post(self.url(), {"text": "Slow payer"}, format="json")
        self.as_user(self.owner)
        self.assertNotIn("Slow payer", self.client.get("/api/auth/me/").content.decode())
        self.assertEqual(self.client.post(self.url(), {"text": "x"}, format="json").status_code, status.HTTP_403_FORBIDDEN)

    def test_too_long(self) -> None:
        self.assertEqual(self.client.post(self.url(), {"text": "x" * 2001}, format="json").status_code,
                         status.HTTP_400_BAD_REQUEST)


class CustomerEmailTests(SubscriberFixture):
    def url(self) -> str:
        return f"{SUBS}{self.owner.id}/emails/"

    def test_send_custom_email(self) -> None:
        self.owner.full_name = "Asha Verma"
        self.owner.save()
        html = (
            '<h2>Your plan</h2><p>Hi, your <strong>Startups</strong> plan is <em>ready</em>.</p>'
            '<ul><li>Install the agent</li><li>Add your team</li></ul>'
            '<p><a href="https://zinwe.io/app">Open dashboard</a></p>'
        )
        response = self.client.post(self.url(), {"subject": "  Welcome   aboard ", "html": html}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        self.assertEqual(response.json()["subject"], "Welcome aboard")
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual((sent.to, sent.subject), ([self.owner.email], "Welcome aboard"))
        self.assertIn("- Install the agent", sent.body)
        self.assertIn("Open dashboard (https://zinwe.io/app)", sent.body)
        html_part = sent.alternatives[0][0]
        self.assertIn("<strong>Startups</strong>", html_part)
        self.assertIn('href="https://zinwe.io/app"', html_part)
        self.assertIn("The Zinwe team", html_part)

    def test_non_breaking_spaces_become_normal(self) -> None:
        self.client.post(self.url(), {"subject": "Hi", "html": "<p>Hi&nbsp;Asha,&nbsp;welcome</p>"}, format="json")
        html_part = mail.outbox[0].alternatives[0][0]
        self.assertIn("<p>Hi Asha, welcome</p>", html_part)

    def test_html_is_cleaned(self) -> None:
        html = ('<p onclick="steal()">Hi<script>alert(1)</script><img src=x onerror="x()">'
                '<a href="javascript:alert(1)">click</a><iframe src="https://evil"></iframe></p>')
        self.client.post(self.url(), {"subject": "Hello", "html": html}, format="json")
        html_part = mail.outbox[0].alternatives[0][0]
        for bad in ("<script", "onclick", 'src="x"', "javascript:", "<iframe", "onerror"):
            self.assertNotIn(bad, html_part)
        self.assertNotIn("<img", html_part)  # the customer's image is gone; the logo is text

    def test_needs_subject_and_message(self) -> None:
        self.assertEqual(self.client.post(self.url(), {"subject": " ", "html": "<p>Hi</p>"}, format="json").status_code,
                         status.HTTP_400_BAD_REQUEST)
        empty = self.client.post(self.url(), {"subject": "Hi", "html": "<p><br></p><script>x</script>"}, format="json")
        self.assertEqual(empty.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(empty.json()["detail"], "Write a message first.")
        self.assertEqual(len(mail.outbox), 0)

    def test_history_lists_only_custom_emails(self) -> None:
        self.client.post(self.url(), {"subject": "First", "html": "<p>One</p>"}, format="json")
        self.client.post(self.url(), {"subject": "Second", "html": "<p>Two</p>"}, format="json")
        Notification.objects.create(channel="email", recipient=self.owner.email, template="welcome", subject="Welcome")
        Notification.objects.create(channel="email", recipient=self.owner.email, template="renewal_reminder",
                                    subject="Your Zinwe Startups plan ends in 7 days", status="sent")
        body = self.client.get(self.url()).json()
        # Custom + automatic renewal emails; account emails like "welcome" stay out.
        self.assertEqual([e["subject"] for e in body], ["Your Zinwe Startups plan ends in 7 days", "Second", "First"])
        self.assertEqual([e["automatic"] for e in body], [True, False, False])
        self.assertEqual(body[1]["status"], "sent")
        # The table row carries the count + last-sent time (welcome emails don't count).
        row = next(r for r in self.client.get(SUBS).json() if r["id"] == self.owner.id)
        self.assertEqual(row["emails"]["sent"], 2)
        self.assertIsNotNone(row["emails"]["last_sent_at"])
        self.assertEqual(self.client.get(f"{SUBS}{self.owner.id}/").json()["emails"]["sent"], 2)

    def test_no_emails_yet(self) -> None:
        row = next(r for r in self.client.get(SUBS).json() if r["id"] == self.owner.id)
        self.assertEqual(row["emails"], {"sent": 0, "last_sent_at": None})

    def test_send_failure_is_reported(self) -> None:
        with mock.patch("z_notification.channels.email.EmailChannel.send", side_effect=OSError("SMTP down")):
            response = self.client.post(self.url(), {"subject": "Hi", "html": "<p>Hello</p>"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("couldn't be sent", response.json()["detail"])

    def test_staff_only(self) -> None:
        self.as_user(self.owner)
        self.assertEqual(self.client.post(self.url(), {"subject": "x", "html": "<p>x</p>"}, format="json").status_code,
                         status.HTTP_403_FORBIDDEN)
