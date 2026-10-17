"""Notification bars: messages pushed from the admin panel, who sees them where, and the upgrade bar."""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from rest_framework import status

from z_auth._testing import give_plan
from z_auth.models import Announcement
from z_devices.models import Device

from .tests import AdminApiTestCase

ADMIN_URL = "/api/admin/announcements/"
OWNER_URL = "/api/auth/announcements/"
PUBLIC_URL = "/api/auth/announcements/public/"


def push(**fields):  # type: ignore[no-untyped-def]
    return Announcement.objects.create(**{"title": "20% off Businesses", "kind": "discount", **fields})


class AdminCrudTests(AdminApiTestCase):
    def test_push_list_edit_and_delete(self) -> None:
        created = self.client.post(
            ADMIN_URL,
            {
                "title": "Founding offer", "body": "First 50 teams", "kind": "offer", "placement": "both",
                "audience": "free", "cta_label": "See plans", "cta_href": "/app/upgrade?plan=startups",
            },
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.content)
        row = created.json()
        self.assertEqual((row["status"], row["created_by"]), ("live", "ops@zinwe.local"))

        self.assertEqual([r["title"] for r in self.client.get(ADMIN_URL).json()], ["Founding offer"])

        off = self.client.patch(f"{ADMIN_URL}{row['id']}/", {"is_active": False}, format="json").json()
        self.assertEqual(off["status"], "off")

        self.assertEqual(self.client.delete(f"{ADMIN_URL}{row['id']}/").status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Announcement.objects.exists())

    def test_rules(self) -> None:
        bad_link = self.client.post(ADMIN_URL, {"title": "x", "cta_label": "Go", "cta_href": "javascript:alert(1)"}, format="json")
        self.assertIn("cta_href", bad_link.json())
        half_button = self.client.post(ADMIN_URL, {"title": "x", "cta_label": "Go"}, format="json")
        self.assertIn("cta_href", half_button.json())
        no_plans = self.client.post(ADMIN_URL, {"title": "x", "audience": "plans"}, format="json")
        self.assertIn("plan_codes", no_plans.json())
        unknown = self.client.post(ADMIN_URL, {"title": "x", "audience": "plans", "plan_codes": ["gold"]}, format="json")
        self.assertIn("plan_codes", unknown.json())
        now = timezone.now()
        backwards = self.client.post(
            ADMIN_URL, {"title": "x", "starts_at": now.isoformat(), "ends_at": (now - timedelta(hours=1)).isoformat()},
            format="json",
        )
        self.assertIn("ends_at", backwards.json())

    def test_staff_only(self) -> None:
        self.as_user(self.make_owner())
        self.assertEqual(self.client.get(ADMIN_URL).status_code, status.HTTP_403_FORBIDDEN)


class WhoSeesWhatTests(AdminApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.owner = self.make_owner()

    def owner_titles(self) -> list[str]:
        self.as_user(self.owner)
        return [a["title"] for a in self.client.get(OWNER_URL).json()["announcements"] if a["kind"] != "upgrade"]

    def public_titles(self) -> list[str]:
        self.client.credentials()
        return [a["title"] for a in self.client.get(PUBLIC_URL).json()["announcements"]]

    def test_placement(self) -> None:
        push(title="Dashboard only", placement="dashboard")
        push(title="Website only", placement="website")
        push(title="Everywhere", placement="both")
        self.assertEqual(sorted(self.owner_titles()), ["Dashboard only", "Everywhere"])
        self.assertEqual(sorted(self.public_titles()), ["Everywhere", "Website only"])

    def test_time_window_and_switch(self) -> None:
        now = timezone.now()
        push(title="Later", starts_at=now + timedelta(days=1))
        push(title="Over", starts_at=now - timedelta(days=2), ends_at=now - timedelta(days=1))
        push(title="Off", is_active=False)
        push(title="Now", ends_at=now + timedelta(days=1))
        self.assertEqual(self.owner_titles(), ["Now"])

    def test_audience(self) -> None:
        push(title="Free", audience="free")
        push(title="Paid", audience="paid")
        push(title="Startups", audience="plans", plan_codes=["startups"])
        self.assertEqual(self.owner_titles(), ["Free"])  # no plan yet = free
        give_plan(self.owner, "startups")
        self.assertEqual(sorted(self.owner_titles()), ["Paid", "Startups"])

    def test_an_edit_shows_again(self) -> None:
        a = push()
        self.as_user(self.owner)
        before = self.client.get(OWNER_URL).json()["announcements"][0]["id"]
        Announcement.objects.filter(pk=a.pk).update(updated_at=timezone.now() + timedelta(minutes=5))
        after = self.client.get(OWNER_URL).json()["announcements"][0]["id"]
        self.assertNotEqual(before, after)


class UpgradeBarTests(AdminApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.owner = self.make_owner()
        self.as_user(self.owner)

    def upgrade(self) -> dict | None:  # type: ignore[type-arg]
        items = [a for a in self.client.get(OWNER_URL).json()["announcements"] if a["kind"] == "upgrade"]
        return items[0] if items else None

    def test_says_what_the_next_plan_adds(self) -> None:
        give_plan(self.owner, "startups")
        bar = self.upgrade()
        # The next plan up is Teams (0024, between Startups and Businesses).
        self.assertEqual(bar["title"], "Upgrade to Teams")
        # Every plan has every feature (0022): the bar says what's bigger.
        self.assertEqual(bar["body"], "You're on Startups. Teams covers up to 25 PCs and 180 days of history.")
        self.assertEqual(bar["cta_href"], "/app/upgrade?plan=teams")

    def test_full_pc_slots_say_so(self) -> None:
        for n in range(3):  # Free forever: 3 PCs
            Device.objects.create(owner=self.owner, machine_id=f"pc-{n}", hostname=f"PC-{n}", token_hash=f"h{n}")
        bar = self.upgrade()
        self.assertEqual(bar["title"], "All 3 PCs on Free forever are in use")
        self.assertEqual(bar["cta_href"], "/app/upgrade?plan=startups")

    def test_nothing_on_the_top_plan(self) -> None:
        give_plan(self.owner, "businesses")
        self.assertIsNone(self.upgrade())
