"""
FR-44: team pictures. A member shows a drawn avatar from the shared library, an uploaded photo, or nothing
(their initials on the role tint, drawn by the client).

- Library: the 16 drawn people in `zinwe-frontend/src/dashboard/team/avatars.json` (byte-identical copy in the
  Windows app). Only the keys live here; keys are stable forever.
- Photo: whatever the client sends (already shrunk to 256 px in the browser / tray) is decoded with Pillow and
  re-encoded here to a 256 x 256 JPEG q82, centre-cropped, with no metadata, so nothing the client smuggles in
  (EXIF/GPS, odd sizes, a polyglot file) is ever stored or served.
- Serving: `GET /api/devices/pictures/<signed>.jpg`, where the token signs (member id, picture version). No auth
  header, so it works in an <img> and in the tray; unguessable; the URL changes whenever the picture does, so it
  can be cached for a year. Never listed and never in plain /media/.
"""
from __future__ import annotations

import io
import secrets
from typing import Any

from django.core import signing
from django.core.files.base import ContentFile
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .models import Member

#: Keys of the drawn library (`avatars.json`). Must match the JSON (test_pictures checks the frontend copy).
AVATAR_KEYS: tuple[str, ...] = tuple(f"a{i:02d}" for i in range(1, 17))

#: Largest photo upload accepted. Clients send ~12-35 KB (256 px JPEG); this leaves room for a q0.7 retry.
MAX_UPLOAD_BYTES = 150 * 1024
#: Largest side of the decoded image (a 4096 px JPEG under 150 KB is possible; anything bigger is refused unread).
MAX_SIDE = 4096
#: What is stored: a 256 x 256 JPEG at quality 82.
OUT_SIDE = 256
JPEG_QUALITY = 82

_SALT = "z_devices.member-picture.v1"
UPLOAD_DIR = "member-pictures"


class PictureRejected(Exception):
    """A picture the API refuses; `code` is the machine reason the clients map to friendly copy."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


# --- Payload -------------------------------------------------------------------------------------------------


def signed_token(member: Member) -> str:
    return signing.Signer(salt=_SALT).sign_object([member.pk, member.picture_version], compress=False)


def member_for_token(token: str) -> Member | None:
    """The member a picture URL points at, only while that exact photo is still theirs; else None (404)."""
    try:
        member_id, version = signing.Signer(salt=_SALT).unsign_object(token)
    except (signing.BadSignature, ValueError, TypeError):
        return None
    member = Member.objects.filter(pk=member_id).first()
    if (
        member is None
        or member.picture_kind != Member.PictureKind.PHOTO
        or member.picture_version != version
        or not member.picture_file
    ):
        return None
    return member


def picture_url(member: Member, request: Any = None) -> str:
    path = reverse("z_devices:picture", kwargs={"token": signed_token(member)})
    return request.build_absolute_uri(path) if request is not None else path


def picture_payload(member: Member | None, request: Any = None) -> dict[str, Any] | None:
    """The `picture` object of every member payload: null, {kind: avatar, key}, or {kind: photo, url, updated_at}."""
    if member is None:
        return None
    if member.picture_kind == Member.PictureKind.AVATAR and member.picture_key in AVATAR_KEYS:
        return {"kind": "avatar", "key": member.picture_key}
    if member.picture_kind == Member.PictureKind.PHOTO and member.picture_file:
        updated = member.picture_updated_at
        return {
            "kind": "photo",
            "url": picture_url(member, request),
            "updated_at": updated.isoformat().replace("+00:00", "Z") if updated else None,
        }
    return None


# --- Writes --------------------------------------------------------------------------------------------------


def _delete_file_later(name: str) -> None:
    """Remove a stored photo once the change that replaced it is committed (never a file still in use)."""
    if not name:
        return
    storage = Member._meta.get_field("picture_file").storage

    def _remove() -> None:
        try:
            storage.delete(name)
        except OSError:  # already gone, or locked on Windows: nothing to serve it anyway
            pass

    transaction.on_commit(_remove)


def _save(member: Member, kind: str, key: str = "", file_name: str = "") -> None:
    old = member.picture_file.name if member.picture_file else ""
    member.picture_kind = kind
    member.picture_key = key
    member.picture_file = file_name
    member.picture_version += 1
    member.picture_updated_at = timezone.now()
    member.save(update_fields=["picture_kind", "picture_key", "picture_file", "picture_version", "picture_updated_at"])
    if old and old != file_name:
        _delete_file_later(old)


@transaction.atomic
def set_avatar(member: Member, key: str) -> None:
    if key not in AVATAR_KEYS:
        raise PictureRejected("unknown_avatar", "That picture isn't in the library.")
    _save(member, Member.PictureKind.AVATAR, key=key)


def encode_photo(data: bytes) -> bytes:
    """Decode an uploaded image and re-encode it to the stored 256 px JPEG, or raise PictureRejected."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    if len(data) > MAX_UPLOAD_BYTES:
        raise PictureRejected("too_large", "That photo is too big. Pick one under 10 MB and try again.")
    if not data:
        raise PictureRejected("not_an_image", "That file isn't a picture.")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            width, height = probe.size  # header only: nothing decoded yet
            if width > MAX_SIDE or height > MAX_SIDE:
                raise PictureRejected("too_big_dimensions", "That photo is too large. Use one under 4096 px a side.")
            if getattr(probe, "is_animated", False) or getattr(probe, "n_frames", 1) > 1:
                raise PictureRejected("not_an_image", "Moving pictures can't be used. Pick a still photo.")
            probe.load()
            img = ImageOps.exif_transpose(probe)  # honour the camera's rotation, then drop the EXIF
            img = img.convert("RGBA") if img.mode in ("P", "LA", "PA") else img
            if img.mode == "RGBA":  # transparent PNG: flatten on white, as the browser does
                flat = Image.new("RGB", img.size, (255, 255, 255))
                flat.paste(img, mask=img.getchannel("A"))
                img = flat
            img = img.convert("RGB")
            img = ImageOps.fit(img, (OUT_SIDE, OUT_SIDE), Image.Resampling.LANCZOS, centering=(0.5, 0.5))
            out = io.BytesIO()
            img.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)  # no exif/icc passed: none stored
            return out.getvalue()
    except PictureRejected:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise PictureRejected("not_an_image", "That file isn't a picture we can read.") from None


@transaction.atomic
def set_photo(member: Member, data: bytes) -> None:
    jpeg = encode_photo(data)
    field = member.picture_file
    name = field.storage.save(f"{UPLOAD_DIR}/{secrets.token_urlsafe(18)}.jpg", ContentFile(jpeg))
    try:
        _save(member, Member.PictureKind.PHOTO, file_name=name)
    except Exception:
        field.storage.delete(name)
        raise


@transaction.atomic
def clear(member: Member) -> None:
    if not member.picture_kind and not member.picture_file:
        return
    _save(member, Member.PictureKind.NONE)


def delete_member_file(member: Member) -> None:
    """pre_delete: a deleted member (or owner account) takes their photo with them, once the delete commits.

    Reads the stored name from the database, not the instance, which may be stale (loaded before the upload).
    """
    name = Member.objects.filter(pk=member.pk).values_list("picture_file", flat=True).first()
    if name:
        _delete_file_later(name)
