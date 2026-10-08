"""
`Authorization: Device <token>` -- auth for the endpoint agent.

Deliberately NOT in DEFAULT_AUTHENTICATION_CLASSES: device views opt in, so a
token stolen from a PC can only call device endpoints, never owner/account APIs.
"""
from __future__ import annotations

from rest_framework import authentication, exceptions, permissions
from rest_framework.request import Request

from .models import Device, hash_device_token


class DeviceTokenAuthentication(authentication.BaseAuthentication):
    keyword = "Device"

    def authenticate(self, request: Request):  # type: ignore[no-untyped-def]
        parts = authentication.get_authorization_header(request).split()
        if not parts or parts[0].decode("latin-1") != self.keyword:
            return None
        if len(parts) != 2:
            raise exceptions.AuthenticationFailed("Invalid device token header.")
        token = parts[1].decode("latin-1")

        device = (
            Device.objects.select_related("owner")
            .filter(token_hash=hash_device_token(token))
            .first()
        )
        if device is None:
            raise exceptions.AuthenticationFailed("Unknown device token.")
        if device.is_revoked:
            raise exceptions.AuthenticationFailed("Device has been removed.")
        if not device.owner.is_active:
            raise exceptions.AuthenticationFailed("Account is disabled.")
        # request.user = the owner, request.auth = the device.
        return device.owner, device

    def authenticate_header(self, request: Request) -> str:
        # Makes DRF answer 401 (not 403), which tells the agent to re-enroll.
        return self.keyword


class IsDevice(permissions.BasePermission):
    def has_permission(self, request: Request, view) -> bool:  # type: ignore[no-untyped-def]
        return isinstance(request.auth, Device)


class IsOwner(permissions.IsAuthenticated):
    """
    A logged-in account (Token/Session), never a PC.

    Owner views already leave DeviceTokenAuthentication out; this is a second lock
    so a future change to the default auth classes can't let a device token in.
    """

    def has_permission(self, request: Request, view) -> bool:  # type: ignore[no-untyped-def]
        return super().has_permission(request, view) and not isinstance(request.auth, Device)
