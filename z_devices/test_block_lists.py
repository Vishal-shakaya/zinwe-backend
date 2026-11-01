"""
Website lists on Rules: what owners and PCs get, masking of hidden names, the allow list, list versions and the build.

Most tests run on a small set of made-up lists in a temp folder; ShippedListTests reads the real data files.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, override_settings
from rest_framework import status

from . import blocklist_build, blocklists
from .management.commands import rebuild_blocklists
from .models import Device, Policy
from .serializers import MAX_ALLOW_DOMAINS
from .services import UNREACHABLE_BYTES
from .test_plan_limits import PlanTestCase
from .tests import CURRENT_POLICY_URL, HEARTBEAT_URL, POLICY_URL, DeviceTestCase

LISTS_URL = "/api/devices/lists/"
BLOCK_SET_URL = "/api/devices/policy/blocklist/"

HIDDEN = ("xvideos-sample.com", "pornhub-sample.com", "tube8.com")
PUBLIC = {
    "gambling": ("bet365.com", "betfair.com", "m.casino-sample.net"),
    "malware": ("free-codec-pack.top",),
    "phishing": ("secure-hdfc-verify.xyz",),
    "social": ("facebook.com",),
    "video": ("youtube.com", "bet365.com"),
}


def write_lists(folder: Path, version: str = "2026.11.01.1", hidden: tuple[str, ...] = HIDDEN) -> None:
    (folder / "adult.txt").write_text("\n".join(hidden) + "\n", "utf-8")
    for key, names in PUBLIC.items():
        (folder / f"{key}.txt").write_text("\n".join(names) + "\n", "utf-8")
    (folder / blocklists.MANIFEST_NAME).write_text(
        json.dumps({"version": version, "built_at": "2026-11-01T06:00:00+00:00"}), "utf-8"
    )


class SampleLists:
    """Points the lists at a temp folder for one test class."""

    def use_sample_lists(self) -> None:
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        write_lists(self.folder)
        patcher = mock.patch.object(blocklists, "DATA_DIR", self.folder)
        patcher.start()
        self.addCleanup(patcher.stop)


class ListsTestCase(SampleLists, DeviceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.use_sample_lists()
        self.token = self.enroll().json()["device_token"]
        self.as_owner()

    def put_web(self, **web: Any) -> Any:
        body = {"enabled": True, "mode": "block", "categories": ["adult", "gambling"], "custom_domains": [], **web}
        return self.client.put(POLICY_URL, {"web": body}, format="json")

    def block_set(self) -> dict[str, Any]:
        self.as_device(self.token)
        body = self.client.get(BLOCK_SET_URL).json()
        self.as_owner()
        return body

    def beat(self) -> dict[str, Any]:
        self.as_device(self.token)
        body = self.client.post(HEARTBEAT_URL, {}, format="json").json()
        self.as_owner()
        return body

    def device_policy(self) -> dict[str, Any]:
        self.as_device(self.token)
        body = self.client.get(CURRENT_POLICY_URL).json()
        self.as_owner()
        return body


class PermissionTests(ListsTestCase):
    def test_a_stranger_gets_nothing(self) -> None:
        self.client.credentials()
        for url in (LISTS_URL, f"{LISTS_URL}adult/sites/", BLOCK_SET_URL):
            self.assertEqual(self.client.get(url).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_a_pc_cannot_read_the_owner_lists(self) -> None:
        self.as_device(self.token)
        self.assertIn(self.client.get(LISTS_URL).status_code, (401, 403))
        self.assertIn(self.client.get(f"{LISTS_URL}adult/sites/").status_code, (401, 403))

    def test_an_owner_cannot_read_the_set_a_pc_gets(self) -> None:
        self.assertIn(self.client.get(BLOCK_SET_URL).status_code, (401, 403))

    def test_unknown_list(self) -> None:
        self.assertEqual(self.client.get(f"{LISTS_URL}nope/sites/").status_code, status.HTTP_404_NOT_FOUND)


class OverviewTests(ListsTestCase):
    def test_true_counts_six_lists_and_which_are_on_by_default(self) -> None:
        body = self.client.get(LISTS_URL).json()
        self.assertEqual(body["version"], "2026.11.01.1")
        rows = {row["key"]: row for row in body["lists"]}
        self.assertEqual(list(rows), ["adult", "gambling", "malware", "phishing", "social", "video"])
        self.assertEqual(rows["adult"]["count"], 3)
        self.assertEqual(rows["gambling"]["count"], 3)
        self.assertTrue(rows["adult"]["masked"])
        self.assertFalse(rows["gambling"]["masked"])
        self.assertEqual([k for k, row in rows.items() if not row["default_on"]], ["social", "video"])

    def test_sites_page_and_search(self) -> None:
        first = self.client.get(f"{LISTS_URL}gambling/sites/?page_size=2").json()
        self.assertEqual(first["count"], 3)
        self.assertTrue(first["has_more"])
        self.assertEqual([s["name"] for s in first["sites"]], ["bet365.com", "betfair.com"])
        self.assertEqual(first["sites"][0]["id"], "bet365.com")
        self.assertEqual(first["sites"][0]["lists"], ["gambling", "video"])

        second = self.client.get(f"{LISTS_URL}gambling/sites/?page_size=2&page=2").json()
        self.assertFalse(second["has_more"])
        self.assertEqual([s["name"] for s in second["sites"]], ["m.casino-sample.net"])

        found = self.client.get(f"{LISTS_URL}gambling/sites/?q=BETF").json()
        self.assertEqual((found["count"], found["sites"][0]["name"]), (1, "betfair.com"))

    def test_bad_paging(self) -> None:
        for query in ("page=0", "page_size=0", f"page_size={blocklists.MAX_PAGE_SIZE + 1}"):
            self.assertEqual(self.client.get(f"{LISTS_URL}gambling/sites/?{query}").status_code, status.HTTP_400_BAD_REQUEST)


class MaskingTests(ListsTestCase):
    def assert_no_full_name(self, response: Any) -> None:
        text = response.content.decode()
        for name in HIDDEN:
            self.assertNotIn(name, text)
            self.assertNotIn(name.split(".")[0], text)

    def test_adult_names_are_masked_with_a_stable_id(self) -> None:
        response = self.client.get(f"{LISTS_URL}adult/sites/")
        self.assert_no_full_name(response)
        rows = {row["name"]: row["id"] for row in response.json()["sites"]}
        self.assertIn("x•••le.com", rows)
        self.assertEqual(rows["x•••le.com"], blocklists.site_id("xvideos-sample.com"))
        self.assertTrue(response.json()["masked"])

    def test_search_never_reaches_past_the_mask(self) -> None:
        # Letters behind the mask find nothing, so a list can't be guessed out letter by letter.
        self.assertEqual(self.client.get(f"{LISTS_URL}adult/sites/?q=vide").json()["count"], 0)
        found = self.client.get(f"{LISTS_URL}adult/sites/?q=x•••")
        self.assertEqual(found.json()["count"], 1)
        self.assert_no_full_name(found)

    def test_the_exact_full_name_finds_its_row_still_masked(self) -> None:
        found = self.client.get(f"{LISTS_URL}adult/sites/?q=xvideos-sample.com")
        self.assertEqual(found.json()["count"], 1)
        self.assert_no_full_name(found)

    def test_unticking_keeps_the_name_out_of_every_owner_response(self) -> None:
        hidden_id = blocklists.site_id("xvideos-sample.com")
        saved = self.put_web(allow_domains=[hidden_id])
        self.assertEqual(saved.status_code, status.HTTP_200_OK)
        self.assert_no_full_name(saved)
        self.assertEqual(saved.json()["web"]["allow_domains"], [hidden_id])
        self.assertEqual(saved.json()["web"]["allow_sites"], [{"id": hidden_id, "name": "x•••le.com", "lists": ["adult"]}])

        for response in (self.client.get(POLICY_URL), self.client.get(LISTS_URL), self.client.get(f"{LISTS_URL}adult/sites/")):
            self.assert_no_full_name(response)
        self.assertNotIn("xvideos", json.dumps(Policy.objects.get(owner=self.owner).web_allow_domains))

    def test_a_full_name_sent_by_hand_is_stored_as_its_id(self) -> None:
        saved = self.put_web(allow_domains=["https://www.xvideos-sample.com/watch"])
        self.assert_no_full_name(saved)
        self.assertEqual(Policy.objects.get(owner=self.owner).web_allow_domains, [blocklists.site_id("xvideos-sample.com")])

    def test_a_plain_allowed_name_that_later_joins_the_hidden_list_stops_showing(self) -> None:
        self.put_web(allow_domains=["newly-hidden.com"])
        write_lists(self.folder, "2026.11.02.1", hidden=(*HIDDEN, "newly-hidden.com"))
        response = self.client.get(POLICY_URL)
        self.assertNotIn("newly-hidden", response.content.decode())
        self.assertEqual(response.json()["web"]["allow_sites"][0]["name"], "n•••en.com")

    def test_a_pc_gets_the_real_name_it_needs(self) -> None:
        self.put_web(allow_domains=[blocklists.site_id("xvideos-sample.com")])
        self.assertEqual(self.device_policy()["web"]["allow_domains"], ["xvideos-sample.com"])

    def test_mask_shapes(self) -> None:
        self.assertEqual(blocklists.mask("xvideos.com"), "x•••os.com")
        self.assertEqual(blocklists.mask("tube8.com"), "t•••8.com")
        self.assertEqual(blocklists.mask("xxx.com"), "x•••.com")
        self.assertEqual(blocklists.mask("example.co.uk"), "e•••le.co.uk")
        self.assertEqual(blocklists.mask("live.cams.example.com"), "l•••le.com")

    def test_an_id_left_behind_by_a_rebuild_shows_no_name_and_blocks_nothing_extra(self) -> None:
        hidden_id = blocklists.site_id("pornhub-sample.com")
        self.put_web(allow_domains=[hidden_id])
        write_lists(self.folder, "2026.11.02.1", hidden=("xvideos-sample.com",))
        row = self.client.get(POLICY_URL).json()["web"]["allow_sites"][0]
        self.assertEqual(row["lists"], [])
        self.assertNotIn("pornhub", row["name"])
        self.assertEqual(self.device_policy()["web"]["allow_domains"], [])


class ResolvedSetTests(ListsTestCase):
    def test_lists_plus_own_sites_by_list(self) -> None:
        self.put_web(custom_domains=["pirate-movies.example"])
        body = self.block_set()
        self.assertEqual(body["version"], self.beat()["web_list_version"])
        self.assertEqual(sorted(body["sites"]), ["adult", "custom", "gambling"])
        self.assertIn("xvideos-sample.com", body["sites"]["adult"])
        self.assertEqual(body["sites"]["custom"], ["pirate-movies.example"])

    def test_allow_list_wins_over_a_list_and_own_sites_are_still_blocked(self) -> None:
        self.put_web(allow_domains=["betfair.com"], custom_domains=["pirate-movies.example"])
        body = self.block_set()
        self.assertNotIn("betfair.com", body["sites"]["gambling"])
        self.assertIn("bet365.com", body["sites"]["gambling"])
        self.assertEqual(body["sites"]["custom"], ["pirate-movies.example"])

    def test_a_hidden_site_unticked_leaves_the_set(self) -> None:
        self.put_web(allow_domains=[blocklists.site_id("tube8.com")])
        self.assertNotIn("tube8.com", self.block_set()["sites"]["adult"])

    def test_a_site_on_two_lists_stays_open_on_both(self) -> None:
        self.put_web(categories=["gambling", "video"], allow_domains=["bet365.com"])
        sites = self.block_set()["sites"]
        self.assertNotIn("bet365.com", sites["gambling"])
        self.assertNotIn("bet365.com", sites["video"])

    def test_allowing_a_site_covers_its_subdomains(self) -> None:
        self.put_web(allow_domains=["casino-sample.net"])
        self.assertNotIn("m.casino-sample.net", self.block_set()["sites"]["gambling"])

    def test_an_own_site_comes_off_the_allow_list(self) -> None:
        # Blocked on purpose beats allowed, and the two lists never overlap on a PC.
        self.put_web(allow_domains=["betfair.com", "other.example"])
        saved = self.put_web(custom_domains=["betfair.com"])
        self.assertEqual(saved.json()["web"]["allow_domains"], ["other.example"])
        self.assertEqual(self.block_set()["sites"]["custom"], ["betfair.com"])

    def test_an_own_site_keeps_a_hidden_name_off_the_allow_list_too(self) -> None:
        self.put_web(allow_domains=[blocklists.site_id("tube8.com")])
        saved = self.put_web(custom_domains=["tube8.com"])
        self.assertEqual(saved.json()["web"]["allow_domains"], [])

    def test_watch_only_blocks_nothing_but_keeps_the_lists(self) -> None:
        self.put_web(mode="monitor", custom_domains=["pirate-movies.example"], allow_domains=["betfair.com"])
        self.assertEqual(self.block_set(), {"version": "", "sites": {}})
        self.assertEqual(self.beat()["web_list_version"], "")
        web = self.client.get(POLICY_URL).json()["web"]
        self.assertEqual((web["custom_domains"], web["allow_domains"]), (["pirate-movies.example"], ["betfair.com"]))

    def test_a_declined_member_blocks_nothing(self) -> None:
        self.put_web()
        Device.objects.update(consent_declined_at=datetime(2026, 11, 1, tzinfo=timezone.utc))
        self.assertEqual(self.block_set(), {"version": "", "sites": {}})

    def test_empty_lists_with_block_means_own_sites_only(self) -> None:
        # The dashboard shows every list off here, so nothing from them may be blocked.
        self.put_web(categories=[], custom_domains=["pirate-movies.example"])
        self.assertEqual(self.block_set()["sites"], {"custom": ["pirate-movies.example"]})


class VersionTests(ListsTestCase):
    def test_the_same_choices_keep_the_version(self) -> None:
        self.put_web()
        first = self.beat()["web_list_version"]
        self.put_web()
        self.assertEqual(self.beat()["web_list_version"], first)
        self.assertEqual(self.device_policy()["web"]["list_version"], first)

    def test_each_choice_moves_it(self) -> None:
        self.put_web()
        seen = {self.beat()["web_list_version"]}
        for change in ({"allow_domains": ["betfair.com"]}, {"custom_domains": ["a.example"]}, {"categories": ["adult"]}):
            self.put_web(**change)
            seen.add(self.beat()["web_list_version"])
        self.assertEqual(len(seen), 4)

    def test_a_new_list_version_reaches_the_pc_without_a_policy_change(self) -> None:
        self.put_web()
        before = self.beat()
        write_lists(self.folder, "2026.11.08.1")
        after = self.beat()
        self.assertNotEqual(after["web_list_version"], before["web_list_version"])
        self.assertEqual(after["policy_version"], before["policy_version"])
        self.assertEqual(self.block_set()["version"], after["web_list_version"])


class ValidationTests(ListsTestCase):
    def test_lists_must_be_known(self) -> None:
        self.assertEqual(self.put_web(categories=["sports"]).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.put_web(categories=["social", "video"]).status_code, status.HTTP_200_OK)

    def test_allow_entries_must_be_sites_or_ids(self) -> None:
        for bad in ("not a site", "localhost", "h:zz", "1.2.3.4"):
            self.assertEqual(self.put_web(allow_domains=[bad]).status_code, status.HTTP_400_BAD_REQUEST, bad)

    def test_allow_list_has_a_size_limit(self) -> None:
        too_many = ["site.example"] * (MAX_ALLOW_DOMAINS + 1)
        response = self.put_web(allow_domains=too_many)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("allow_domains", response.json()["web"])

    def test_upload_limit_cannot_be_negative(self) -> None:
        body = self.client.get(POLICY_URL).json()["data_transfer"]
        response = self.client.put(POLICY_URL, {"data_transfer": {**body, "web_upload_warn_bytes": -1}}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_repeats_collapse_and_saving_the_same_again_changes_nothing(self) -> None:
        first = self.put_web(allow_domains=["betfair.com", "www.betfair.com", "BETFAIR.COM"]).json()
        self.assertEqual(first["web"]["allow_domains"], ["betfair.com"])
        again = self.put_web(allow_domains=["betfair.com"]).json()
        self.assertEqual(again["version"], first["version"])


class UploadLimitTests(ListsTestCase):
    def test_the_limit_is_a_real_setting(self) -> None:
        body = self.client.get(POLICY_URL).json()["data_transfer"]
        self.assertEqual(body["web_upload_warn_bytes"], Policy.DEFAULT_UPLOAD_WARN_BYTES)
        saved = self.client.put(POLICY_URL, {"data_transfer": {**body, "web_upload_warn_bytes": 500_000_000}}, format="json")
        self.assertEqual(saved.json()["data_transfer"]["web_upload_warn_bytes"], 500_000_000)
        self.assertEqual(self.device_policy()["data_transfer"]["web_upload_warn_bytes"], 500_000_000)

    def test_an_older_dashboard_leaves_it_and_the_allow_list_alone(self) -> None:
        self.put_web(allow_domains=["betfair.com"])
        body = self.client.get(POLICY_URL).json()["data_transfer"]
        self.client.put(POLICY_URL, {"data_transfer": {**body, "web_upload_warn_bytes": 500_000_000}}, format="json")

        del body["web_upload_warn_bytes"]
        self.client.put(POLICY_URL, {"data_transfer": {**body, "usb_warn_bytes": 1}}, format="json")
        saved = self.client.put(
            POLICY_URL, {"web": {"enabled": True, "mode": "block", "categories": ["adult"], "custom_domains": []}}, format="json"
        ).json()

        self.assertEqual(saved["data_transfer"]["web_upload_warn_bytes"], 500_000_000)
        self.assertEqual(saved["web"]["allow_domains"], ["betfair.com"])


class OlderAgentTests(ListsTestCase):
    def test_the_fields_an_older_agent_reads_are_unchanged(self) -> None:
        self.put_web(categories=["adult", "gambling"], custom_domains=["pirate-movies.example"])
        web = self.device_policy()["web"]
        self.assertEqual(web["enabled"], True)
        self.assertEqual(web["mode"], "block")
        self.assertEqual(web["categories"], ["adult", "gambling"])
        self.assertEqual(web["custom_domains"], ["pirate-movies.example"])

    def test_an_older_heartbeat_still_works(self) -> None:
        self.as_device(self.token)
        response = self.client.post(HEARTBEAT_URL, {"agent_version": "1.3.6"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("policy_version", response.json())


class PlanTests(SampleLists, PlanTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.use_sample_lists()
        self.token = self.enroll().json()["device_token"]
        Policy.objects.create(owner=self.owner, web_mode="block", web_categories=["adult"], web_custom_domains=["a.example"])

    def test_a_plan_without_websites_has_no_lists(self) -> None:
        self.use_plan(None)
        self.as_owner()
        for url in (LISTS_URL, f"{LISTS_URL}adult/sites/"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
            self.assertEqual(response.json()["code"], "plan_feature")
            self.assertEqual(response.json()["module"], "web")

    def test_a_pc_on_that_plan_blocks_nothing_and_has_no_upload_limit(self) -> None:
        self.use_plan(None)
        self.as_device(self.token)
        self.assertEqual(self.client.get(BLOCK_SET_URL).json(), {"version": "", "sites": {}})
        policy = self.client.get(CURRENT_POLICY_URL).json()
        self.assertEqual(policy["web"]["list_version"], "")
        self.assertEqual(policy["data_transfer"]["web_upload_warn_bytes"], UNREACHABLE_BYTES)
        self.assertEqual(self.client.post(HEARTBEAT_URL, {}, format="json").json()["web_list_version"], "")

    def test_a_plan_with_websites_gets_them(self) -> None:
        self.use_plan("startups")
        self.as_owner()
        self.assertEqual(self.client.get(LISTS_URL).status_code, status.HTTP_200_OK)
        self.as_device(self.token)
        self.assertIn("adult", self.client.get(BLOCK_SET_URL).json()["sites"])


class ShippedListTests(SimpleTestCase):
    """The real data files."""

    def setUp(self) -> None:
        self.safe = frozenset(blocklists.load_lines(blocklists.DATA_DIR / "safelist.txt"))
        self.lists = {key: blocklists.load_lines(blocklists.DATA_DIR / f"{key}.txt") for key in blocklists.LIST_KEYS}

    def test_manifest_matches_the_files(self) -> None:
        manifest = json.loads((blocklists.DATA_DIR / blocklists.MANIFEST_NAME).read_text("utf-8"))
        self.assertRegex(manifest["version"], r"^\d{4}\.\d{2}\.\d{2}\.\d+$")
        for key, names in self.lists.items():
            self.assertEqual(manifest["lists"][key]["count"], len(names), key)

    def test_sizes_are_curated(self) -> None:
        for key, names in self.lists.items():
            self.assertGreaterEqual(len(names), 30, key)
            self.assertLessEqual(len(names), 5000, key)
            self.assertEqual(len(names), len(set(names)), key)

    def test_a_site_is_on_one_list_so_the_totals_on_rules_add_up(self) -> None:
        names = [name for values in self.lists.values() for name in values]
        self.assertEqual(len(names), len(set(names)))

    def test_every_entry_is_a_clean_host(self) -> None:
        for key, names in self.lists.items():
            for name in names:
                self.assertEqual(blocklists.clean_host(name) or "", name.removeprefix("www."), f"{key}: {name}")

    def test_no_common_legitimate_site_is_on_a_list_built_from_sources(self) -> None:
        for key in ("adult", "gambling", "malware", "phishing"):
            hits = [name for name in self.lists[key] if blocklists.covered_by(name, self.safe)]
            self.assertEqual(hits, [], key)

    def test_the_named_safe_sites_are_really_in_the_safelist(self) -> None:
        for name in (
            "www.google.com", "mail.google.com", "login.microsoftonline.com", "mail.zoho.in", "tallysolutions.com",
            "services.gst.gov.in", "incometax.gov.in", "netbanking.hdfcbank.com", "onlinesbi.sbi", "icicibank.com",
            "razorpay.com", "paytm.com", "s3.amazonaws.com", "storage.azure.com", "digitalocean.com",
        ):
            self.assertIsNotNone(blocklists.covered_by(name, self.safe), name)

    def test_sources_are_recorded(self) -> None:
        sources = (blocklists.DATA_DIR / "SOURCES.md").read_text("utf-8")
        for url_part in ("Sinfonietta", "ShadowWhisperer", "CERT Polska", "Majestic", "URLhaus"):
            self.assertIn(url_part, sources)

    def test_hand_lists_cover_the_big_names(self) -> None:
        self.assertIn("facebook.com", self.lists["social"])
        self.assertIn("instagram.com", self.lists["social"])
        self.assertIn("youtube.com", self.lists["video"])
        self.assertIn("netflix.com", self.lists["video"])


PSL = b"// comment\ncom\nin\nco.in\ngithub.io\n*.ck\n!www.ck\n"
MAJESTIC = b"GlobalRank,TldRank,Domain\n1,1,google.com\n2,2,bigbet.com\n3,3,hacked-popular.com\n4,4,pornsite.com\n5,5,onelist.com\n"
CERT = (
    "PozycjaRejestru\tAdresDomeny\tDataWpisu\tDataWykreslenia\n"
    "1\tnew-phish.xyz\t2026-10-30T10:00:00+00:00\t\n"
    "2\told-phish.xyz\t2026-01-01T10:00:00+00:00\t\n"
    "3\tgone-phish.xyz\t2026-10-30T10:00:00+00:00\t2026-10-31T10:00:00+00:00\n"
    "4\thacked-popular.com\t2026-10-31T10:00:00+00:00\t\n"
).encode()


class BuildTests(SimpleTestCase):
    def fetch(self, url: str) -> bytes:
        return {
            blocklist_build.PSL_URL: PSL,
            blocklist_build.MAJESTIC_URL: MAJESTIC,
            blocklist_build.SINFONIETTA_PORN_URL: b"0.0.0.0 pornsite.com\n0.0.0.0 www.pornsite.com\n0.0.0.0 onelist.com\n0.0.0.0 google.com\n",
            blocklist_build.TIUXO_PORN_URL: b"",
            blocklist_build.SHADOW_BASE_URL + "Adult": b"pornsite.com\ngoogle.com\n",
            blocklist_build.SINFONIETTA_GAMBLING_URL: b"0.0.0.0 bigbet.com\n",
            blocklist_build.SHADOW_BASE_URL + "Gambling": b"bigbet.com\n",
            blocklist_build.SHADOW_BASE_URL + "Malware": b"dead-malware.top\nlive-malware.top\nhacked-popular.com\nsomething.github.io\ngithub.io\n",
            blocklist_build.DIGITALSIDE_URL: b"# header\nfresh-malware.top\n",
            blocklist_build.CERT_PL_URL: CERT,
            blocklist_build.DURABLE_URL: b"0.0.0.0 scam-shop.top\n",
            blocklist_build.SHADOW_BASE_URL + "Scam": b"",
        }[url]

    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        write_lists(self.folder, "2026.10.01.1")
        (self.folder / "safelist.txt").write_text("google.com\n", "utf-8")
        (self.folder / "gambling-hand.txt").write_text("# hand\nhandbet.com\n", "utf-8")
        self.now = datetime(2026, 11, 1, tzinfo=timezone.utc)

    def run_build(self, **kw: Any) -> blocklist_build.BuildResult:
        kw.setdefault("resolves", lambda host: "dead" not in host)
        return blocklist_build.build(self.fetch, data_dir=self.folder, now=self.now, **kw)

    def lines(self, key: str) -> list[str]:
        return blocklists.load_lines(self.folder / f"{key}.txt")

    def test_threat_lists_drop_popular_safe_shared_and_dead_names(self) -> None:
        result = self.run_build()
        self.assertEqual(self.lines("malware"), ["fresh-malware.top", "live-malware.top", "something.github.io"])
        self.assertEqual(self.lines("phishing"), ["new-phish.xyz", "scam-shop.top"])
        self.assertEqual(result.changed, ["malware", "phishing"])
        self.assertEqual(result.version, "2026.11.01.1")

    def test_well_known_needs_agreement_or_a_telling_name_and_a_rank(self) -> None:
        self.run_build(only=("adult", "gambling"))
        self.assertEqual(self.lines("adult"), ["pornsite.com"])  # google.com safelisted, onelist.com one source only
        self.assertEqual(self.lines("gambling"), ["bigbet.com", "handbet.com"])

    def test_hand_lists_are_left_alone(self) -> None:
        before = self.lines("social")
        self.run_build(only=("adult", "gambling", "malware", "phishing"))
        self.assertEqual(self.lines("social"), before)

    def test_a_second_build_the_same_day_counts_up_and_no_change_keeps_the_version(self) -> None:
        self.run_build()
        self.assertEqual(self.run_build().changed, [])
        manifest = json.loads((self.folder / blocklists.MANIFEST_NAME).read_text("utf-8"))
        self.assertEqual(manifest["version"], "2026.11.01.1")
        self.assertEqual(blocklist_build.next_version("2026.11.01.1", self.now), "2026.11.01.2")
        self.assertEqual(blocklist_build.next_version("2026.10.30.4", self.now), "2026.11.01.1")

    def test_an_empty_source_never_wipes_a_list(self) -> None:
        before = self.lines("malware")
        with self.assertRaises(ValueError):
            self.run_build(resolves=lambda host: False)
        self.assertEqual(self.lines("malware"), before)

    def test_a_dry_run_writes_nothing(self) -> None:
        before = (self.folder / "malware.txt").read_bytes()
        self.run_build(write=False)
        self.assertEqual((self.folder / "malware.txt").read_bytes(), before)

    def test_a_cut_off_majestic_file_stops_the_build(self) -> None:
        before = self.lines("malware")
        with self.assertRaises(ValueError):
            self.run_build(min_ranks=1000)
        self.assertEqual(self.lines("malware"), before)

    def test_a_rebuild_of_every_sourced_list_is_dated(self) -> None:
        self.run_build()
        manifest = json.loads((self.folder / blocklists.MANIFEST_NAME).read_text("utf-8"))
        self.assertEqual(manifest["built_at"], "2026-11-01T00:00:00+00:00")
        self.assertNotEqual(manifest["full_built_at"], manifest["built_at"])
        self.run_build(only=blocklist_build.ALL_SOURCED, resolves=lambda host: True)
        manifest = json.loads((self.folder / blocklists.MANIFEST_NAME).read_text("utf-8"))
        self.assertEqual(manifest["full_built_at"], manifest["built_at"])

    def test_public_suffixes(self) -> None:
        psl = blocklist_build.PublicSuffixes.parse(PSL)
        self.assertEqual(psl.registrable("a.b.example.co.in"), "example.co.in")
        self.assertEqual(psl.registrable("me.github.io"), "me.github.io")
        self.assertTrue(psl.is_suffix("github.io"))
        self.assertFalse(psl.is_suffix("me.github.io"))
        self.assertEqual(psl.suffix_of("a.b.ck"), "b.ck")
        self.assertEqual(psl.suffix_of("www.ck"), "ck")

    def test_the_command_runs_and_writes_nothing_on_a_dry_run(self) -> None:
        with mock.patch("z_devices.management.commands.rebuild_blocklists.download", self.fetch), \
                mock.patch.object(blocklist_build.build, "__kwdefaults__", {**blocklist_build.build.__kwdefaults__, "data_dir": self.folder}):
            call_command("rebuild_blocklists", "--dry-run", "--no-dns", verbosity=0, stdout=mock.MagicMock())


class PublishTests(SimpleTestCase):
    """The weekly run on a server: lists go live outside the release, and only when they pass."""

    fetch = BuildTests.fetch

    def setUp(self) -> None:
        self.shipped = Path(tempfile.mkdtemp())
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.shipped, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        write_lists(self.shipped, "2026.11.01.1", hidden=("tube8.com",))
        (self.shipped / "safelist.txt").write_text("google.com\n", "utf-8")
        (self.shipped / "gambling-hand.txt").write_text("# hand\nhandbet.com\n", "utf-8")
        for patcher in (
            mock.patch.object(blocklists, "DATA_DIR", self.shipped),
            mock.patch.object(blocklist_build, "DATA_DIR", self.shipped),
            mock.patch.object(blocklist_build, "MIN_RANKS", 1),
            override_settings(BLOCKLIST_DIR=str(self.root)),
        ):
            (patcher.enable if hasattr(patcher, "enable") else patcher.start)()
            self.addCleanup(patcher.disable if hasattr(patcher, "disable") else patcher.stop)
        self.now = datetime(2026, 11, 8, tzinfo=timezone.utc)

    def run_publish(self, **kw: Any) -> blocklist_build.BuildResult:
        kw.setdefault("resolves", lambda host: "dead" not in host)
        kw.setdefault("now", self.now)
        fetch = kw.pop("fetch", self.fetch)
        return blocklist_build.publish(fetch, self.root, **kw)

    def assert_nothing_went_live(self) -> None:
        self.assertEqual(blocklists.version(), "2026.11.01.1")
        self.assertEqual(list(self.root.glob(".stage-*")), [])
        self.assertFalse((self.root / blocklists.CURRENT_NAME).exists())

    def test_goes_live_without_a_release_or_a_restart(self) -> None:
        self.assertEqual(blocklists.version(), "2026.11.01.1")
        result = self.run_publish()
        self.assertEqual(result.version, "2026.11.08.1")
        self.assertEqual(blocklists.version(), "2026.11.08.1")
        self.assertIn("new-phish.xyz", blocklists.resolve(["phishing"], [], [])["phishing"])
        self.assertEqual(blocklists.load_lines(self.shipped / "phishing.txt"), list(PUBLIC["phishing"]))
        self.assertEqual(list(self.root.glob(".stage-*")), [])

    def test_lists_it_did_not_rebuild_come_across_whole(self) -> None:
        self.run_publish()
        self.assertEqual(blocklists.resolve(["social"], [], [])["social"], list(PUBLIC["social"]))
        self.assertEqual(blocklists.resolve(["adult"], [], [])["adult"], ["tube8.com"])

    def test_nothing_changed_stays_on_the_same_build(self) -> None:
        self.run_publish()
        result = self.run_publish(now=self.now + timedelta(days=7))
        self.assertEqual(result.changed, [])
        self.assertEqual(blocklists.version(), "2026.11.08.1")
        self.assertEqual(list(self.root.glob(".stage-*")), [])

    def test_a_list_that_shrinks_too_far_is_refused(self) -> None:
        names = [f"old-{n}.top" for n in range(10)]
        (self.shipped / "malware.txt").write_text("\n".join(names) + "\n", "utf-8")
        with self.assertRaisesRegex(ValueError, "malware list fell from 10 to 3"):
            self.run_publish()
        self.assert_nothing_went_live()

    def test_a_source_that_fails_leaves_the_live_lists(self) -> None:
        def broken(url: str) -> bytes:
            if url == blocklist_build.CERT_PL_URL:
                raise OSError("timed out")
            return self.fetch(url)

        with self.assertRaises(OSError):
            self.run_publish(fetch=broken)
        self.assert_nothing_went_live()

    def test_dns_going_down_leaves_the_live_lists(self) -> None:
        with self.assertRaises(ValueError):
            self.run_publish(resolves=lambda host: False)
        self.assert_nothing_went_live()

    def test_a_cut_off_majestic_file_is_refused(self) -> None:
        with mock.patch.object(blocklist_build, "MIN_RANKS", 500_000), self.assertRaisesRegex(ValueError, "Majestic"):
            self.run_publish()
        self.assert_nothing_went_live()

    def test_a_release_with_newer_lists_wins_and_the_next_run_builds_on_it(self) -> None:
        self.run_publish()
        write_lists(self.shipped, "2026.11.09.1", hidden=("tube8.com",))
        manifest = json.loads((self.shipped / blocklists.MANIFEST_NAME).read_text("utf-8"))
        manifest["built_at"] = "2026-11-09T00:00:00+00:00"
        (self.shipped / blocklists.MANIFEST_NAME).write_text(json.dumps(manifest), "utf-8")
        self.assertEqual(blocklists.version(), "2026.11.09.1")
        self.run_publish(now=self.now + timedelta(days=7))
        self.assertEqual(blocklists.version(), "2026.11.15.1")

    def test_a_safelist_edit_in_the_release_reaches_the_next_run(self) -> None:
        self.run_publish()
        (self.shipped / "safelist.txt").write_text("google.com\nnew-phish.xyz\n", "utf-8")
        with mock.patch.object(blocklist_build, "MIN_KEEP_SHARE", 0):
            self.run_publish(now=self.now + timedelta(days=7))
        self.assertNotIn("new-phish.xyz", blocklists.resolve(["phishing"], [], [])["phishing"])

    def test_adult_and_gambling_join_every_90_days(self) -> None:
        self.assertEqual(self.run_publish().changed, ["malware", "phishing"])
        full = datetime(2026, 11, 1, 6, tzinfo=timezone.utc)  # The shipped lists' build.
        with mock.patch.object(blocklist_build, "MIN_KEEP_SHARE", 0):  # The sample phishing names age out by then.
            self.assertNotIn("adult", self.run_publish(now=full + timedelta(days=89)).changed)
            result = self.run_publish(now=full + timedelta(days=90))
        self.assertEqual(result.changed[:2], ["adult", "gambling"])
        manifest = json.loads((self.root / result.version / blocklists.MANIFEST_NAME).read_text("utf-8"))
        self.assertEqual(manifest["full_built_at"], manifest["built_at"])

    def test_old_builds_are_cleared_but_the_live_one_and_a_few_stay(self) -> None:
        for week in range(8):
            now = self.now + timedelta(days=7 * week)
            cert = f"head\n1\tnew-{week}.xyz\t{now.isoformat()}\t\n".encode()
            self.run_publish(
                fetch=lambda url, cert=cert: cert if url == blocklist_build.CERT_PL_URL else self.fetch(url), now=now
            )
        builds = sorted(item.name for item in self.root.iterdir() if item.is_dir())
        self.assertEqual(len(builds), blocklist_build.KEEP_BUILDS)
        self.assertIn(blocklists.version(), builds)

    def test_a_bad_pointer_falls_back_to_the_release(self) -> None:
        (self.root / blocklists.CURRENT_NAME).write_text("2020.01.01.1\n", "utf-8")
        self.assertEqual(blocklists.version(), "2026.11.01.1")

    def test_the_command_needs_a_folder_to_publish_to(self) -> None:
        with override_settings(BLOCKLIST_DIR=""), self.assertRaisesRegex(CommandError, "ZINWE_BLOCKLIST_DIR"):
            call_command("rebuild_blocklists", "--publish")

    def test_the_command_reports_a_refused_rebuild_as_a_failure(self) -> None:
        with mock.patch.object(blocklist_build, "publish", side_effect=ValueError("fell")):
            with self.assertRaisesRegex(CommandError, "fell"):
                call_command("rebuild_blocklists", "--publish")

    def test_a_download_is_tried_again(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"ok"
        with mock.patch("urllib.request.urlopen", side_effect=[OSError("reset"), response]) as opened:
            with mock.patch.object(rebuild_blocklists.time, "sleep"):
                self.assertEqual(rebuild_blocklists.download("https://example.com/list"), b"ok")
        self.assertEqual(opened.call_count, 2)
