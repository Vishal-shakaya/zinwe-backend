"""The public website contact form: POST /api/contact/ (J1-01)."""
from __future__ import annotations

from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from z_analytics.models import Event

CONTACT_URL = "/api/contact/"
FORM = {
    "name": "Rohit Kulkarni",
    "email": "Rohit@Kulkarni.in",
    "company": "Kulkarni & Associates",
    "phone": "+91 98200 00000",
    "topic": "Pricing",
    "message": "Will this work for 12 PCs?",
}


@override_settings(ZINWE_TEAM_NOTIFY_EMAIL="team@zinwe.test")
class ContactTests(APITestCase):
    def setUp(self) -> None:
        cache.clear()

    def send(self, data: dict):  # type: ignore[no-untyped-def,type-arg]
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(CONTACT_URL, data, format="json")

    def test_message_reaches_the_team_and_the_visitor_hears_back(self) -> None:
        response = self.send(FORM)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        self.assertEqual(response.json(), {"status": "sent"})
        self.assertEqual(len(mail.outbox), 2)
        team, visitor = mail.outbox
        self.assertEqual(team.to, ["team@zinwe.test"])
        self.assertEqual(team.reply_to, ["rohit@kulkarni.in"])  # "Reply" answers the visitor
        self.assertEqual(team.subject, "Website message from Rohit Kulkarni (Kulkarni & Associates): Pricing")
        self.assertIn("Will this work for 12 PCs?", team.body)
        self.assertIn("+91 98200 00000", team.body)
        self.assertEqual(visitor.to, ["rohit@kulkarni.in"])
        self.assertEqual(visitor.subject, "We got your message — Zinwe")
        self.assertIn("Hi Rohit", visitor.body)
        self.assertIn("within one working day", visitor.body)
        self.assertTrue(Event.objects.filter(name="contact.sent").exists())

    def test_only_name_email_and_message_are_required(self) -> None:
        response = self.send({"name": "Asha", "email": "asha@acme.in", "message": "Hello"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("Company:  -", mail.outbox[0].body)

    def test_field_errors(self) -> None:
        response = self.send({"email": "not-an-email"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        body = response.json()
        self.assertEqual(set(body), {"name", "email", "message"})
        self.assertEqual(body["name"], ["This field is required."])
        self.assertEqual(body["email"], ["Enter a valid email address."])
        self.assertEqual(len(mail.outbox), 0)

    def test_email_without_a_domain_ending_is_refused(self) -> None:
        response = self.send({**FORM, "email": "rohit@kulkarni"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", response.json())  # J1-05: the reply could never arrive

    def test_public_no_login_needed_and_get_not_allowed(self) -> None:
        self.client.credentials(HTTP_AUTHORIZATION="Token not-a-real-token")  # a stale token doesn't matter
        self.assertEqual(self.send(FORM).status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.client.get(CONTACT_URL).status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_throttled_after_10_an_hour(self) -> None:
        for _ in range(10):
            self.assertEqual(self.send(FORM).status_code, status.HTTP_201_CREATED)
        response = self.send(FORM)
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertIn("retry_after", response.json())
