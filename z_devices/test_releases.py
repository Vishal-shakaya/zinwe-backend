"""Agent releases: publish from the admin app, offer on heartbeat, required pauses work, device-only download."""
from __future__ import annotations

import hashlib
import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework import status
from rest_framework.authtoken.models import Token

from z_auth.models import User

from .models import AgentRelease
from .services import _parse_version, pending_update
from .tests import HEARTBEAT_URL, DeviceTestCase

RELEASES_URL = "/api/admin/releases/"
MSI = b"MSI-BYTES-" * 1000

_media = tempfile.mkdtemp(prefix="zinwe-releases-")


@override_settings(MEDIA_ROOT=_media, ZINWE_AGENT_MIN_VERSION="")
class ReleaseTests(DeviceTestCase):
    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        shutil.rmtree(_media, ignore_errors=True)

    def setUp(self) -> None:
        super().setUp()
        self.token = self.enroll(agent_version="0.2.0").json()["device_token"]
        self.staff = User.objects.create_superuser("ops@zinwe.local", "ops-12345678")

    # --- helpers ---------------------------------------------------------------

    def as_staff(self) -> None:
        token, _ = Token.objects.get_or_create(user=self.staff)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

    def publish(self, version: str, required: bool = True, **extra):  # type: ignore[no-untyped-def]
        self.as_staff()
        body = {"version": version, "notes": f"What's new in {version}", "required": required, **extra}
        if "download_url" not in extra:
            body["file"] = SimpleUploadedFile(f"Zinwe-{version}.msi", MSI, content_type="application/x-msi")
        return self.client.post(RELEASES_URL, body, format="multipart")

    def beat(self, version: str = "0.2.0") -> dict:
        self.as_device(self.token)
        response = self.client.post(HEARTBEAT_URL, {"agent_version": version}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        return response.json()

    # --- versions --------------------------------------------------------------

    def test_versions_compare_like_people_expect(self) -> None:
        self.assertEqual(_parse_version("0.3"), _parse_version("0.3.0"))
        self.assertEqual(_parse_version("0.3.0+59a6133"), _parse_version("0.3.0"))  # build metadata ignored
        self.assertLess(_parse_version("0.9.0"), _parse_version("0.10.0"))
        self.assertLess(_parse_version(""), _parse_version("0.0.1"))

    # --- publishing ------------------------------------------------------------

    def test_publishing_an_upload_computes_hash_and_size(self) -> None:
        response = self.publish("0.3.0")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        row = response.json()
        self.assertEqual(row["sha256"], hashlib.sha256(MSI).hexdigest())
        self.assertEqual((row["size"], row["hosted"], row["required"]), (len(MSI), "upload", True))
        self.assertEqual(row["adoption"], {"updated": 0, "total": 1})
        self.assertEqual(self.client.get(RELEASES_URL).json()[0]["version"], "0.3.0")

    def test_bad_releases_are_refused(self) -> None:
        self.assertEqual(self.publish("three").status_code, status.HTTP_400_BAD_REQUEST)
        self.publish("0.3.0")
        self.assertIn("already published", self.publish("0.3.0").json()["detail"])
        self.as_staff()
        not_msi = self.client.post(RELEASES_URL, {"version": "0.4.0", "file": SimpleUploadedFile("x.exe", b"x")},
                                   format="multipart")
        self.assertEqual(not_msi.status_code, status.HTTP_400_BAD_REQUEST)
        no_hash = self.publish("0.4.0", download_url="https://cdn.zinwe.io/ZinweAgent-0.4.0.msi")
        self.assertEqual(no_hash.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_external_link_needs_its_hash(self) -> None:
        response = self.publish("0.4.0", download_url="https://cdn.zinwe.io/a.msi", sha256="A" * 64)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        self.assertEqual(self.beat()["update"]["download_url"], "https://cdn.zinwe.io/a.msi")

    def test_only_staff_can_publish(self) -> None:
        self.as_owner()
        self.assertEqual(self.client.get(RELEASES_URL).status_code, status.HTTP_403_FORBIDDEN)

    # --- the offer on heartbeat ------------------------------------------------

    def test_up_to_date_agents_are_not_offered_anything(self) -> None:
        self.publish("0.2.0")
        beat = self.beat("0.2.0")
        self.assertNotIn("update", beat)
        self.assertNotIn("update_required", beat)

    def test_a_required_release_pauses_older_agents_and_says_where_to_get_it(self) -> None:
        release_id = self.publish("0.3.0").json()["id"]
        beat = self.beat("0.2.0")
        self.assertTrue(beat["update_required"])
        update = beat["update"]
        self.assertEqual((update["version"], update["required"]), ("0.3.0", True))
        self.assertEqual(update["download_url"], f"/api/devices/releases/{release_id}/download/")
        self.assertEqual(update["sha256"], hashlib.sha256(MSI).hexdigest())

    def test_an_optional_release_is_offered_without_pausing_work(self) -> None:
        self.publish("0.3.0", required=False)
        beat = self.beat("0.2.0")
        self.assertEqual(beat["update"]["required"], False)
        self.assertNotIn("update_required", beat)

    def test_skipping_a_required_release_does_not_dodge_it(self) -> None:
        self.publish("0.3.0", required=True)
        self.publish("0.4.0", required=False)
        update = pending_update("0.2.0")
        assert update is not None
        self.assertEqual((update["version"], update["required"]), ("0.4.0", True))  # newest, but still required
        self.assertFalse(pending_update("0.3.0")["required"])  # type: ignore[index]

    def test_a_withdrawn_release_is_no_longer_offered(self) -> None:
        release_id = self.publish("0.3.0").json()["id"]
        self.as_staff()
        self.client.patch(f"{RELEASES_URL}{release_id}/", {"is_active": False}, format="json")
        self.assertNotIn("update", self.beat("0.2.0"))

    def test_staff_can_make_a_release_optional(self) -> None:
        release_id = self.publish("0.3.0").json()["id"]
        self.as_staff()
        row = self.client.patch(f"{RELEASES_URL}{release_id}/", {"required": False}, format="json").json()
        self.assertFalse(row["required"])
        self.assertNotIn("update_required", self.beat("0.2.0"))

    @override_settings(ZINWE_AGENT_MIN_VERSION="0.5.0")
    def test_the_env_minimum_still_forces_an_update(self) -> None:
        beat = self.beat("0.2.0")
        self.assertTrue(beat["update_required"])
        self.assertTrue(beat["update"]["required"])

    def test_updated_agents_count_towards_adoption(self) -> None:
        self.publish("0.3.0")
        self.beat("0.3.0")
        self.as_staff()
        self.assertEqual(self.client.get(RELEASES_URL).json()[0]["adoption"], {"updated": 1, "total": 1})

    # --- the download ----------------------------------------------------------

    def test_only_enrolled_pcs_can_download_the_installer(self) -> None:
        release_id = self.publish("0.3.0").json()["id"]
        url = f"/api/devices/releases/{release_id}/download/"

        self.as_owner()
        self.assertEqual(self.client.get(url).status_code, status.HTTP_401_UNAUTHORIZED)

        self.as_device(self.token)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(b"".join(response.streaming_content), MSI)
        self.assertIn("ZinweAgent-0.3.0.msi", response["Content-Disposition"])

        AgentRelease.objects.filter(pk=release_id).update(is_active=False)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)
