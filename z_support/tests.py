"""Customer support: owner opens/replies from the dashboard; staff answer in the admin app."""
from __future__ import annotations

from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from z_auth.models import User

from .models import SupportMessage, SupportTicket

OWNER_URL = "/api/support/tickets/"
STAFF_URL = "/api/admin/support/"


@override_settings(ZINWE_TEAM_NOTIFY_EMAIL="team@zinwe.test", ZINWE_ADMIN_URL="https://admin.zinwe.test")
class SupportTests(APITestCase):
    def setUp(self) -> None:
        cache.clear()
        self.owner = User.objects.create_user("asha@acme.in", "pw-12345678", full_name="Asha Verma",
                                              email_verified_at=timezone.now())
        self.staff = User.objects.create_superuser("ops@zinwe.local", "ops-12345678")

    def as_user(self, user: User) -> None:
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

    def post(self, url: str, body: dict):  # type: ignore[no-untyped-def]
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(url, body, format="json")

    def open(self) -> dict:
        self.as_user(self.owner)
        response = self.post(OWNER_URL, {"subject": "USB block not working", "category": "problem",
                                         "message": "Pen drive still copies on ACCOUNTS-PC.", "page": "/app/data"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        return response.json()

    def test_open_emails_the_team(self) -> None:
        ticket = self.open()
        self.assertEqual(ticket["status"], "open")
        self.assertEqual(len(ticket["messages"]), 1)
        self.assertEqual(mail.outbox[0].to, ["team@zinwe.test"])
        self.assertIn("New support request", mail.outbox[0].subject)
        self.assertIn(f"https://admin.zinwe.test/support?ticket={ticket['id']}", mail.outbox[0].body)

    def test_staff_reply_emails_owner_and_shows_unread(self) -> None:
        ticket = self.open()
        mail.outbox.clear()
        self.as_user(self.staff)
        listing = self.client.get(STAFF_URL).json()
        self.assertEqual(listing["counts"]["open"], 1)
        self.assertEqual(listing["rows"][0]["unread"], 1)
        detail = self.client.get(f"{STAFF_URL}{ticket['id']}/").json()
        self.assertEqual(detail["customer"]["email"], "asha@acme.in")
        self.assertEqual(self.client.get(STAFF_URL).json()["rows"][0]["unread"], 0)  # opened = read

        reply = self.post(f"{STAFF_URL}{ticket['id']}/reply/", {"body": "Please update the agent to 0.2."})
        self.assertEqual(reply.status_code, status.HTTP_200_OK, reply.content)
        self.assertEqual(reply.json()["status"], "answered")
        self.assertEqual(mail.outbox[0].to, ["asha@acme.in"])
        self.assertIn("Please update the agent to 0.2.", mail.outbox[0].body)

        self.as_user(self.owner)
        mine = self.client.get(OWNER_URL).json()
        self.assertEqual(mine[0]["unread"], 1)
        thread = self.client.get(f"{OWNER_URL}{ticket['id']}/").json()
        self.assertEqual([m["sender"] for m in thread["messages"]], ["owner", "staff"])
        self.assertEqual(thread["messages"][1]["author_name"], "Zinwe team")
        self.assertEqual(self.client.get(OWNER_URL).json()[0]["unread"], 0)

    def test_owner_reply_reopens(self) -> None:
        ticket = self.open()
        SupportTicket.objects.filter(pk=ticket["id"]).update(status="closed")
        response = self.post(f"{OWNER_URL}{ticket['id']}/messages/", {"body": "Still happening."})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(SupportTicket.objects.get().status, "open")

    def test_staff_can_close_without_reply(self) -> None:
        ticket = self.open()
        self.as_user(self.staff)
        response = self.post(f"{STAFF_URL}{ticket['id']}/status/", {"status": "closed"})
        self.assertEqual(response.json()["status"], "closed")

    def test_owners_only_see_their_own(self) -> None:
        ticket = self.open()
        other = User.objects.create_user("rafi@else.in", "pw-12345678")
        self.as_user(other)
        self.assertEqual(self.client.get(OWNER_URL).json(), [])
        self.assertEqual(self.client.get(f"{OWNER_URL}{ticket['id']}/").status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(
            self.post(f"{OWNER_URL}{ticket['id']}/messages/", {"body": "x"}).status_code, status.HTTP_404_NOT_FOUND
        )
        self.assertEqual(self.client.get(STAFF_URL).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(SupportMessage.objects.count(), 1)

    def test_required_fields(self) -> None:
        self.as_user(self.owner)
        response = self.post(OWNER_URL, {"subject": "", "message": ""})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertNotIn("subject", response.json())  # optional: the chat has no subject box
        self.assertIn("message", response.json())

    def test_subject_comes_from_the_message_when_left_out(self) -> None:
        self.as_user(self.owner)
        short = self.post(OWNER_URL, {"category": "question", "message": "\n  How do I add a PC?\nThanks"})
        self.assertEqual(short.status_code, status.HTTP_201_CREATED, short.content)
        self.assertEqual(short.json()["subject"], "How do I add a PC?")
        long_msg = "The USB block rule is on but the pen drive still copies files on the accounts PC every time"
        long = self.post(OWNER_URL, {"message": long_msg}).json()
        self.assertTrue(long["subject"].endswith("…"))
        self.assertLessEqual(len(long["subject"]), 61)
        self.assertTrue(long_msg.startswith(long["subject"][:-1]))
        self.assertIn(long["subject"], mail.outbox[-1].subject)
