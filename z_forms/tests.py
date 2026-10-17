"""The website forms inbox: every form is stored, and staff see it in admin -> Forms (/api/admin/forms/)."""
from __future__ import annotations

from django.core.cache import cache
from django.test import override_settings
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from z_auth.models import User

from .kinds import label_for
from .models import FormSubmission
from .services import record_submission

FORMS_URL = "/api/admin/forms/"
CONTACT = {
    "name": "Rohit Kulkarni",
    "email": "Rohit@Kulkarni.in",
    "company": "Kulkarni & Associates",
    "phone": "+91 98200 00000",
    "topic": "Pricing",
    "message": "Will this work for 12 PCs?",
}
SETUP_CALL = {
    "name": "Asha Rao",
    "email": "asha@acme.in",
    "company": "Acme",
    "phone": "",
    "team_size": "11-25",
    "platforms": ["windows"],
    "concerns": ["usb", "email"],
}


@override_settings(ZINWE_TEAM_NOTIFY_EMAIL="team@zinwe.test")
class RecordingTests(APITestCase):
    def setUp(self) -> None:
        cache.clear()

    def post(self, url: str, data: dict):  # type: ignore[no-untyped-def,type-arg]
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(url, data, format="json")

    def test_contact_form_is_stored(self) -> None:
        self.assertEqual(self.post("/api/contact/", CONTACT).status_code, status.HTTP_201_CREATED)
        sub = FormSubmission.objects.get()
        self.assertEqual(sub.kind, "contact")
        self.assertEqual(sub.email, "rohit@kulkarni.in")
        self.assertEqual(sub.subject, "Pricing")
        self.assertEqual(sub.message, "Will this work for 12 PCs?")
        self.assertEqual(sub.company, "Kulkarni & Associates")
        self.assertEqual(sub.status, FormSubmission.Status.NEW)

    def test_invalid_contact_form_stores_nothing(self) -> None:
        self.assertEqual(self.post("/api/contact/", {"email": "x"}).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(FormSubmission.objects.exists())

    def test_setup_call_is_stored_every_time(self) -> None:
        self.assertEqual(self.post("/api/auth/early-access/", SETUP_CALL).status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.post("/api/auth/early-access/", SETUP_CALL).status_code, status.HTTP_200_OK)
        first, repeat = FormSubmission.objects.order_by("id")
        self.assertEqual(first.kind, "setup_call")
        self.assertEqual(first.details["team_size"], "11-25")
        self.assertEqual(first.details["platforms"], ["Windows"])
        self.assertEqual(first.details["concerns"], ["USB / removable drives", "Email data leaks"])
        self.assertFalse(first.details["booked_before"])
        self.assertTrue(repeat.details["booked_before"])


class StaffInboxTests(APITestCase):
    def setUp(self) -> None:
        self.staff = User.objects.create_user(email="ops@zinwe.test", password="x-Strong-pass-1", is_staff=True)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=self.staff).key}")
        self.contact = record_submission("contact", name="Rohit", email="rohit@kulkarni.in", message="Pricing?")
        self.call = record_submission("setup_call", name="Asha", email="asha@acme.in", company="Acme")
        self.other = record_submission("pricing_question", name="Neel", email="neel@x.in", message="Discount?")

    def test_only_staff(self) -> None:
        owner = User.objects.create_user(email="owner@acme.in", password="x-Strong-pass-1")
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=owner).key}")
        self.assertEqual(self.client.get(FORMS_URL).status_code, status.HTTP_403_FORBIDDEN)
        self.client.credentials()
        self.assertEqual(self.client.get(FORMS_URL).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_lists_everything_with_a_tab_per_kind(self) -> None:
        body = self.client.get(FORMS_URL).json()
        self.assertEqual([r["id"] for r in body["rows"]], [self.other.id, self.call.id, self.contact.id])
        kinds = {k["kind"]: k for k in body["kinds"]}
        # Known forms first (in order), then any other kind that has submissions.
        self.assertEqual([k["kind"] for k in body["kinds"]], ["contact", "setup_call", "pricing_question"])
        self.assertEqual(kinds["contact"], {"kind": "contact", "label": "Contact", "total": 1, "new": 1})
        self.assertEqual(kinds["pricing_question"]["label"], "Pricing question")
        self.assertEqual(body["counts"], {"new": 3, "handled": 0})
        self.assertEqual(body["rows"][0]["kind_label"], "Pricing question")

    def test_known_kinds_show_even_when_empty(self) -> None:
        FormSubmission.objects.all().delete()
        body = self.client.get(FORMS_URL).json()
        self.assertEqual([(k["kind"], k["total"]) for k in body["kinds"]], [("contact", 0), ("setup_call", 0)])

    def test_filter_by_kind_status_and_search(self) -> None:
        rows = self.client.get(FORMS_URL, {"kind": "setup_call"}).json()["rows"]
        self.assertEqual([r["id"] for r in rows], [self.call.id])
        self.client.patch(f"{FORMS_URL}{self.contact.id}/", {"status": "handled"}, format="json")
        rows = self.client.get(FORMS_URL, {"status": "new"}).json()["rows"]
        self.assertNotIn(self.contact.id, [r["id"] for r in rows])
        rows = self.client.get(FORMS_URL, {"q": "acme"}).json()["rows"]
        self.assertEqual([r["id"] for r in rows], [self.call.id])
        counts = self.client.get(FORMS_URL, {"kind": "contact"}).json()["counts"]
        self.assertEqual(counts, {"new": 0, "handled": 1})

    def test_mark_handled_reopen_and_note(self) -> None:
        url = f"{FORMS_URL}{self.contact.id}/"
        body = self.client.patch(url, {"status": "handled", "note": "  Called him back  "}, format="json").json()
        self.assertEqual(body["status"], "handled")
        self.assertEqual(body["note"], "Called him back")
        self.assertEqual(body["handled_by"], "ops@zinwe.test")
        self.assertIsNotNone(body["handled_at"])
        body = self.client.patch(url, {"status": "new"}, format="json").json()
        self.assertEqual((body["status"], body["handled_by"], body["handled_at"]), ("new", "", None))
        self.assertEqual(body["note"], "Called him back")  # the note stays
        self.assertEqual(self.client.patch(url, {"status": "nope"}, format="json").status_code, 400)
        self.assertEqual(self.client.get(f"{FORMS_URL}{self.contact.id}/").json()["id"], self.contact.id)

    def test_sidebar_badge_counts_new_forms(self) -> None:
        self.assertEqual(self.client.get("/api/admin/badges/").json()["forms_new"], 3)

    def test_label_for_unknown_kinds(self) -> None:
        self.assertEqual(label_for("demo-call"), "Demo call")
        self.assertEqual(label_for("setup_call"), "Setup call")
