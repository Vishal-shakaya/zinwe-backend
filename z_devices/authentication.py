"""
`Authorization: Device <token>` -- auth for the endpoint agent.

Deliberately NOT in DEFAULT_AUTHENTICATION_CLASSES: device views opt in, so a
token stolen from a PC can only call device endpoints, never owner/account APIs.
"""
from __future__ import annotations

from rest_framework import authentication, exceptions, permissions
from rest_framework.request import Request
from rest_framework.throttling import ScopedRateThrottle

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



class DeviceScopedThrottle(ScopedRateThrottle):
    """
    `throttle_scope` per enrolled PC (FIX-006), not per IP: an office of 50 PCs behind one address must not share
    a limit. Runs after authentication + IsDevice, so `request.auth` is the Device; anything else falls back to
    the client IP. Rates: settings REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] (device_heartbeat / device_events /
    device_metrics), each overridable by env.
    """

    def get_cache_key(self, request: Request, view) -> str | None:  # type: ignore[no-untyped-def]
        device = request.auth
        ident = f"device-{device.pk}" if isinstance(device, Device) else self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}
