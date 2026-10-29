"""FR-44: team pictures -- library avatars, photo upload (re-encoded), signed serving, payloads, permissions."""
from __future__ import annotations

import io
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image
from rest_framework import status

from z_auth.models import User

from . import pictures
from .authentication import DeviceScopedThrottle
from .models import Member
from .test_tasks import TASKS_URL
from .tests import HEARTBEAT_URL, MEMBERS_URL, DeviceTestCase

ME_PICTURE_URL = "/api/devices/me/picture/"

# The dashboard's library (source of truth). Reachable when the backend sits next to the frontend checkout
# (the main repo layout, or the zinwe-wt worktrees); the hard-coded list below is the fallback.
_FRONTEND_JSON_CANDIDATES = [
    Path(settings.BASE_DIR).parent / "zinwe-frontend" / "src" / "dashboard" / "team" / "avatars.json",
    Path(settings.BASE_DIR).parent / "frontend-pic" / "src" / "dashboard" / "team" / "avatars.json",
    Path(settings.BASE_DIR).parent / "_shared" / "avatars.json",
]
KNOWN_KEYS = [
    "a01", "a02", "a03", "a04", "a05", "a06", "a07", "a08",
    "a09", "a10", "a11", "a12", "a13", "a14", "a15", "a16",
]


def picture_url(member: Member) -> str:
    return f"{MEMBERS_URL}{member.id}/picture/"


def image_bytes(fmt: str = "JPEG", size: tuple[int, int] = (300, 200), color=(200, 90, 30), **save: Any) -> bytes:
    out = io.BytesIO()
    mode = "RGBA" if fmt == "PNG" and save.pop("alpha", False) else "RGB"
    Image.new(mode, size, color if mode == "RGB" else (*color, 120)).save(out, fmt, **save)
    return out.getvalue()


def jpeg_with_gps() -> bytes:
    img = Image.new("RGB", (320, 320), (10, 120, 200))
    exif = Image.Exif()
    exif[0x010F] = "SecretCam"  # Make
    exif[0x8825] = {1: "N", 2: (12.0, 58.0, 0.0)}  # GPS IFD
    out = io.BytesIO()
    img.save(out, "JPEG", exif=exif.tobytes())
    return out.getvalue()


def animated_gif() -> bytes:
    frames = [Image.new("RGB", (64, 64), c) for c in ((255, 0, 0), (0, 0, 255))]
    out = io.BytesIO()
    frames[0].save(out, "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    return out.getvalue()


class PictureTestCase(DeviceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._media = tempfile.mkdtemp(prefix="zinwe-pictures-")
        self._override = override_settings(MEDIA_ROOT=self._media)
        self._override.enable()

    def tearDown(self) -> None:
        self._override.disable()
        shutil.rmtree(self._media, ignore_errors=True)
        super().tearDown()

    def put_avatar(self, member: Member | None = None, key: str = "a07"):  # type: ignore[no-untyped-def]
        self.as_owner()
        return self.client.put(picture_url(member or self.member), {"kind": "avatar", "key": key}, format="json")

    def put_photo(self, data: bytes, member: Member | None = None, url: str | None = None, name: str = "me.jpg"):  # type: ignore[no-untyped-def]
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.put(
                url or picture_url(member or self.member),
                {"file": SimpleUploadedFile(name, data, content_type="image/jpeg")},
                format="multipart",
            )

    def stored_files(self) -> list[Path]:
        root = Path(self._media) / pictures.UPLOAD_DIR
        return sorted(root.iterdir()) if root.exists() else []

    def fetch(self, url: str):  # type: ignore[no-untyped-def]
        self.client.credentials()  # no auth header: works in an <img>
        return self.client.get(url.replace("http://testserver", ""))


class LibraryTests(PictureTestCase):
    def test_avatar_keys_match_the_dashboard_library(self) -> None:
        self.assertEqual(list(pictures.AVATAR_KEYS), KNOWN_KEYS)
        found = [p for p in _FRONTEND_JSON_CANDIDATES if p.exists()]
        if not found:
            self.skipTest("frontend avatars.json not reachable from this checkout; the hard-coded list was checked")
        library = json.loads(found[0].read_text(encoding="utf-8"))
        self.assertEqual([a["key"] for a in library["avatars"]], KNOWN_KEYS)
        self.assertEqual(library["viewBox"], "0 0 48 48")


class OwnerAvatarTests(PictureTestCase):
    def test_owner_picks_a_library_avatar(self) -> None:
        response = self.put_avatar(key="a07")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertEqual(response.json(), {"picture": {"kind": "avatar", "key": "a07"}})
        self.member.refresh_from_db()
        self.assertEqual((self.member.picture_kind, self.member.picture_key), ("avatar", "a07"))
        self.assertEqual(self.member.picture_version, 1)

    def test_unknown_avatar_is_refused(self) -> None:
        for key in ("kite", "a17", "", "a1"):
            response = self.put_avatar(key=key)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(response.json()["code"], "unknown_avatar")
        self.member.refresh_from_db()
        self.assertEqual(self.member.picture_kind, "")

    def test_bad_body_is_refused(self) -> None:
        self.as_owner()
        response = self.client.put(picture_url(self.member), {"kind": "photo"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["code"], "bad_request")

    def test_delete_goes_back_to_initials(self) -> None:
        self.put_avatar()
        response = self.client.delete(picture_url(self.member))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.member.refresh_from_db()
        self.assertEqual((self.member.picture_kind, self.member.picture_key), ("", ""))
        self.assertIsNone(self.client.get(MEMBERS_URL).json()[0]["picture"])

    def test_another_owners_member_is_not_found(self) -> None:
        other = User.objects.create_user("boss@other.in", "pw-12345678")
        theirs = Member.objects.create(owner=other, name="Ravi", email="ravi@other.in")
        self.assertEqual(self.put_avatar(member=theirs).status_code, status.HTTP_404_NOT_FOUND)
        self.as_owner()
        self.assertEqual(self.put_photo(image_bytes(), member=theirs).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.delete(picture_url(theirs)).status_code, status.HTTP_404_NOT_FOUND)
        theirs.refresh_from_db()
        self.assertEqual(theirs.picture_kind, "")

    def test_needs_an_owner_login_not_a_device_token(self) -> None:
        token = self.enroll().json()["device_token"]
        self.as_device(token)
        response = self.client.put(picture_url(self.member), {"kind": "avatar", "key": "a01"}, format="json")
        self.assertIn(response.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))
        self.client.credentials()
        response = self.client.put(picture_url(self.member), {"kind": "avatar", "key": "a01"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_writes_are_throttled_per_owner(self) -> None:
        rates = {**settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], "member_picture": "2/hour"}
        from rest_framework.throttling import ScopedRateThrottle

        original = ScopedRateThrottle.THROTTLE_RATES
        ScopedRateThrottle.THROTTLE_RATES = rates
        try:
            self.assertEqual(self.put_avatar(key="a01").status_code, 200)
            self.assertEqual(self.put_avatar(key="a02").status_code, 200)
            self.assertEqual(self.put_avatar(key="a03").status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        finally:
            ScopedRateThrottle.THROTTLE_RATES = original


class PhotoTests(PictureTestCase):
    def test_upload_is_re_encoded_to_a_256px_jpeg_without_metadata(self) -> None:
        self.as_owner()
        response = self.put_photo(jpeg_with_gps())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        picture = response.json()["picture"]
        self.assertEqual(picture["kind"], "photo")
        self.assertTrue(picture["url"].startswith("http://testserver/api/devices/pictures/"))
        self.assertTrue(picture["url"].endswith(".jpg"))
        self.assertTrue(picture["updated_at"].endswith("Z"))
        [stored] = self.stored_files()
        with Image.open(stored) as img:
            self.assertEqual((img.format, img.size, img.mode), ("JPEG", (256, 256), "RGB"))
            self.assertEqual(len(img.getexif()), 0)
            self.assertNotIn("exif", img.info)
        self.assertNotIn(b"SecretCam", stored.read_bytes())

    def test_png_with_transparency_and_a_wide_image_are_accepted(self) -> None:
        self.as_owner()
        self.assertEqual(self.put_photo(image_bytes("PNG", (500, 120), alpha=True), name="a.png").status_code, 200)
        self.assertEqual(self.put_photo(image_bytes("WEBP", (120, 500)), name="a.webp").status_code, 200)
        with Image.open(self.stored_files()[0]) as img:
            self.assertEqual(img.size, (256, 256))

    def test_not_an_image(self) -> None:
        self.as_owner()
        for data in (b"%PDF-1.7 not a picture", b"MZ\x90\x00 windows exe", b"\x00"):
            response = self.put_photo(data, name="x.jpg")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(response.json()["code"], "not_an_image")
        self.assertEqual(self.stored_files(), [])

    def test_empty_multipart_is_not_an_image(self) -> None:
        self.as_owner()
        response = self.client.put(picture_url(self.member), {"other": "x"}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["code"], "not_an_image")

    def test_too_large(self) -> None:
        self.as_owner()
        big = image_bytes(size=(64, 64)) + b"\x00" * (pictures.MAX_UPLOAD_BYTES + 1)
        response = self.put_photo(big)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["code"], "too_large")
        self.assertEqual(self.stored_files(), [])

    def test_huge_dimensions(self) -> None:
        self.as_owner()
        # A 5000 x 10 PNG of one colour is a few hundred bytes: small file, too many pixels a side.
        response = self.put_photo(image_bytes("PNG", (5000, 10)), name="wide.png")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["code"], "too_big_dimensions")

    def test_animated_gif_is_refused(self) -> None:
        self.as_owner()
        response = self.put_photo(animated_gif(), name="party.gif")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["code"], "not_an_image")
        # a still GIF is fine
        self.assertEqual(self.put_photo(image_bytes("GIF", (80, 80)), name="still.gif").status_code, 200)


class ServingTests(PictureTestCase):
    def upload(self) -> str:
        self.as_owner()
        response = self.put_photo(image_bytes(size=(600, 400)))
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["picture"]["url"]

    def test_signed_url_serves_the_jpeg_without_auth_and_caches_for_a_year(self) -> None:
        url = self.upload()
        response = self.fetch(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertEqual(response["Cache-Control"], "private, max-age=31536000, immutable")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        body = b"".join(response.streaming_content)
        self.assertTrue(body.startswith(b"\xff\xd8"))

    def test_tampered_token_is_404(self) -> None:
        url = self.upload()
        token = url.rsplit("/", 1)[1][: -len(".jpg")]
        value, sig = token.rsplit(":", 1)
        forged = pictures.signing.Signer(salt="something-else").sign_object([self.member.pk, 1], compress=False)
        for bad in (value + ":" + sig[::-1], value[:-2] + "x" + value[-1:] + ":" + sig, forged, "nope", ""):
            self.assertEqual(self.fetch(f"/api/devices/pictures/{bad}.jpg").status_code, 404, bad)

    def test_old_url_is_404_after_a_change_and_the_old_file_is_deleted(self) -> None:
        first = self.upload()
        [old_file] = self.stored_files()
        second = self.upload()
        self.assertNotEqual(first, second)
        self.assertEqual(self.fetch(first).status_code, 404)
        self.assertEqual(self.fetch(second).status_code, 200)
        self.assertFalse(old_file.exists())
        self.assertEqual(len(self.stored_files()), 1)

    def test_switching_to_an_avatar_or_removing_deletes_the_file(self) -> None:
        url = self.upload()
        with self.captureOnCommitCallbacks(execute=True):
            self.put_avatar(key="a03")
        self.assertEqual(self.stored_files(), [])
        self.assertEqual(self.fetch(url).status_code, 404)

        url = self.upload()
        self.as_owner()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.delete(picture_url(self.member)).status_code, 204)
        self.assertEqual(self.stored_files(), [])
        self.assertEqual(self.fetch(url).status_code, 404)

    def test_deleted_member_is_404_and_takes_the_file(self) -> None:
        url = self.upload()
        with self.captureOnCommitCallbacks(execute=True):
            self.member.delete()
        self.assertEqual(self.stored_files(), [])
        self.assertEqual(self.fetch(url).status_code, 404)

    def test_owner_account_delete_takes_every_photo(self) -> None:
        self.upload()
        with self.captureOnCommitCallbacks(execute=True):
            self.owner.delete()
        self.assertEqual(self.stored_files(), [])

    def test_missing_file_on_disk_is_404(self) -> None:
        url = self.upload()
        for f in self.stored_files():
            f.unlink()
        self.assertEqual(self.fetch(url).status_code, 404)

    def test_not_served_from_media(self) -> None:
        self.upload()
        [stored] = self.stored_files()
        self.client.credentials()
        self.assertEqual(self.client.get(f"/media/{pictures.UPLOAD_DIR}/{stored.name}").status_code, 404)


class DevicePictureTests(PictureTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.token = self.enroll().json()["device_token"]

    def test_device_sets_reads_and_removes_its_own_members_picture(self) -> None:
        self.as_device(self.token)
        self.assertEqual(self.client.get(ME_PICTURE_URL).json(), {"picture": None})
        response = self.client.put(ME_PICTURE_URL, {"kind": "avatar", "key": "a12"}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"picture": {"kind": "avatar", "key": "a12"}})

        response = self.put_photo(image_bytes(), url=ME_PICTURE_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["picture"]["kind"], "photo")
        self.assertEqual(self.client.get(ME_PICTURE_URL).json(), response.json())

        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.delete(ME_PICTURE_URL).status_code, 204)
        self.assertEqual(self.stored_files(), [])
        self.member.refresh_from_db()
        self.assertEqual(self.member.picture_kind, "")

    def test_owner_sees_what_the_pc_set(self) -> None:
        self.as_device(self.token)
        self.client.put(ME_PICTURE_URL, {"kind": "avatar", "key": "a05"}, format="json")
        self.as_owner()
        [row] = self.client.get(MEMBERS_URL).json()
        self.assertEqual(row["picture"], {"kind": "avatar", "key": "a05"})

    def test_device_errors_use_the_same_codes(self) -> None:
        self.as_device(self.token)
        self.assertEqual(
            self.client.put(ME_PICTURE_URL, {"kind": "avatar", "key": "zz"}, format="json").json()["code"],
            "unknown_avatar",
        )
        self.assertEqual(self.put_photo(b"not an image", url=ME_PICTURE_URL).json()["code"], "not_an_image")

    def test_a_pc_can_only_change_its_own_member(self) -> None:
        # Another member's PC (same owner) changes only its own member, never Asha.
        other = Member.objects.create(owner=self.owner, name="Kiran", email="kiran@acme.in")
        token = self.enroll(email=other.email, member_id=other.member_code, machine_id="6F1C2A40-0000-4000-8000-000000000002").json()["device_token"]
        self.as_device(token)
        self.client.put(ME_PICTURE_URL, {"kind": "avatar", "key": "a09"}, format="json")
        self.member.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.member.picture_kind, "")
        self.assertEqual(other.picture_key, "a09")
        # ...and a device token can't reach the owner route for anyone.
        response = self.client.put(picture_url(self.member), {"kind": "avatar", "key": "a01"}, format="json")
        self.assertIn(response.status_code, (401, 403))
        self.member.refresh_from_db()
        self.assertEqual(self.member.picture_kind, "")

    def test_unlinked_pc_is_409(self) -> None:
        from .models import Device

        Device.objects.update(member=None)
        self.as_device(self.token)
        response = self.client.put(ME_PICTURE_URL, {"kind": "avatar", "key": "a01"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.json()["code"], "no_member")

    def test_no_token_is_401(self) -> None:
        self.client.credentials()
        self.assertEqual(self.client.put(ME_PICTURE_URL, {"kind": "avatar", "key": "a01"}, format="json").status_code, 401)

    def test_device_writes_are_throttled_per_pc_but_reads_are_not(self) -> None:
        original = DeviceScopedThrottle.THROTTLE_RATES
        DeviceScopedThrottle.THROTTLE_RATES = {**original, "device_picture": "1/hour"}
        try:
            self.as_device(self.token)
            self.assertEqual(self.client.put(ME_PICTURE_URL, {"kind": "avatar", "key": "a01"}, format="json").status_code, 200)
            self.assertEqual(self.client.put(ME_PICTURE_URL, {"kind": "avatar", "key": "a02"}, format="json").status_code, 429)
            for _ in range(3):
                self.assertEqual(self.client.get(ME_PICTURE_URL).status_code, 200)
        finally:
            DeviceScopedThrottle.THROTTLE_RATES = original

    def test_heartbeat_carries_the_picture(self) -> None:
        self.as_device(self.token)
        body = {"agent_version": "0.1.0"}
        self.assertIsNone(self.client.post(HEARTBEAT_URL, body, format="json").json()["picture"])
        self.put_avatar(key="a04")
        self.as_device(self.token)
        self.assertEqual(
            self.client.post(HEARTBEAT_URL, body, format="json").json()["picture"], {"kind": "avatar", "key": "a04"}
        )


class PayloadTests(PictureTestCase):
    def test_members_list_detail_and_writes_carry_picture(self) -> None:
        self.as_owner()
        self.assertIsNone(self.client.get(MEMBERS_URL).json()[0]["picture"])
        self.put_avatar(key="a10")
        self.as_owner()
        self.assertEqual(self.client.get(MEMBERS_URL).json()[0]["picture"], {"kind": "avatar", "key": "a10"})
        patched = self.client.patch(f"{MEMBERS_URL}{self.member.id}/", {"role": "sales"}, format="json").json()
        self.assertEqual(patched["picture"], {"kind": "avatar", "key": "a10"})
        created = self.client.post(MEMBERS_URL, {"name": "New", "email": "new@acme.in"}, format="json").json()
        self.assertIsNone(created["picture"])

    def test_photo_url_in_the_list_is_absolute_and_fetchable(self) -> None:
        self.as_owner()
        self.put_photo(image_bytes())
        row = self.client.get(MEMBERS_URL).json()[0]
        self.assertEqual(row["picture"]["kind"], "photo")
        self.assertTrue(row["picture"]["url"].startswith("http://testserver/"))
        self.assertEqual(self.fetch(row["picture"]["url"]).status_code, 200)

    def test_tasks_carry_the_assignees_picture(self) -> None:
        self.put_avatar(key="a02")
        self.as_owner()
        task = self.client.post(f"{MEMBERS_URL}{self.member.id}/tasks/", {"title": "Ship it"}, format="json").json()
        self.assertEqual(task["member_picture"], {"kind": "avatar", "key": "a02"})
        [row] = self.client.get(TASKS_URL).json()
        self.assertEqual(row["member_picture"], {"kind": "avatar", "key": "a02"})

    def test_picture_payload_shapes(self) -> None:
        m = self.member
        self.assertIsNone(pictures.picture_payload(None))
        self.assertIsNone(pictures.picture_payload(m))
        m.picture_kind, m.picture_key = "avatar", "retired-key"
        self.assertIsNone(pictures.picture_payload(m))  # an unknown key reads as initials, never breaks a client
        m.picture_key = "a01"
        self.assertEqual(pictures.picture_payload(m), {"kind": "avatar", "key": "a01"})
