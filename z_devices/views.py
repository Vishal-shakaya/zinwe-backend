"""Thin HTTP layer for z_devices. Validation in serializers, logic in services."""
from __future__ import annotations

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from z_main.client import get_client_ip

from . import services
from .authentication import DeviceTokenAuthentication, IsDevice, IsOwner
from .serializers import (
    DevicePolicySerializer,
    DeviceSerializer,
    EnrollmentKeySerializer,
    EnrollSerializer,
    EventFeedQuerySerializer,
    EventSerializer,
    EventUploadSerializer,
    HeartbeatSerializer,
    PolicySerializer,
    PolicyUpdateSerializer,
)

# --- Agent endpoints (called by the Windows agent) --------------------------


class EnrollView(APIView):
    """POST /api/devices/enroll/ -- exchange an enrollment key for a device token."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "device_enroll"

    def post(self, request: Request) -> Response:
        serializer = EnrollSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            device, token, created = services.enroll_device(
                serializer.validated_data, ip=get_client_ip(request)
            )
        except services.EnrollmentRejected as exc:
            return Response({"enrollment_key": [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            {"device_id": device.id, "device_token": token, "re_enrolled": not created},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class HeartbeatView(APIView):
    """POST /api/devices/heartbeat/ -- `Authorization: Device <token>`."""

    authentication_classes = [DeviceTokenAuthentication]
    permission_classes = [IsDevice]

    def post(self, request: Request) -> Response:
        serializer = HeartbeatSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            services.record_heartbeat(request.auth, serializer.validated_data, get_client_ip(request))
        )


class CurrentPolicyView(APIView):
    """GET /api/devices/policy/current/ -- the rules this PC should enforce."""

    authentication_classes = [DeviceTokenAuthentication]
    permission_classes = [IsDevice]

    def get(self, request: Request) -> Response:
        policy = services.get_policy(request.auth.owner)
        return Response(DevicePolicySerializer(policy).data)


class EventUploadView(APIView):
    """POST /api/devices/events/upload/ -- 1..100 queued events, idempotent on `id`."""

    authentication_classes = [DeviceTokenAuthentication]
    permission_classes = [IsDevice]

    def post(self, request: Request) -> Response:
        serializer = EventUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        accepted, duplicates = services.store_events(
            request.auth, serializer.validated_data["events"]
        )
        return Response({"accepted": accepted, "duplicates": duplicates})


# --- Owner endpoints (called by the dashboard) ------------------------------


class EnrollmentKeyView(APIView):
    """GET /api/devices/enrollment-key/ -- the owner's active key (created on first call)."""

    permission_classes = [IsOwner]

    def get(self, request: Request) -> Response:
        return Response(EnrollmentKeySerializer(services.get_active_key(request.user)).data)


class EnrollmentKeyRotateView(APIView):
    """POST /api/devices/enrollment-key/rotate/ -- revoke the key and issue a new one."""

    permission_classes = [IsOwner]

    def post(self, request: Request) -> Response:
        key = services.rotate_key(request.user)
        return Response(EnrollmentKeySerializer(key).data, status=status.HTTP_201_CREATED)


class DeviceListView(APIView):
    """GET /api/devices/ -- the owner's enrolled PCs with online status."""

    permission_classes = [IsOwner]

    def get(self, request: Request) -> Response:
        return Response(DeviceSerializer(request.user.devices.all(), many=True).data)


class PolicyView(APIView):
    """GET/PUT /api/devices/policy/ -- the owner's rules for all their PCs."""

    permission_classes = [IsOwner]

    def get(self, request: Request) -> Response:
        return Response(PolicySerializer(services.get_policy(request.user)).data)

    def put(self, request: Request) -> Response:
        serializer = PolicyUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        policy = services.update_policy(request.user, serializer.validated_data)
        return Response(PolicySerializer(policy).data)


class EventFeedView(APIView):
    """GET /api/devices/events/?device=<id>&limit=<1..200> -- newest first, own PCs only."""

    permission_classes = [IsOwner]

    def get(self, request: Request) -> Response:
        query = EventFeedQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        events = services.events_for_owner(
            request.user,
            device_id=query.validated_data.get("device"),
            limit=query.validated_data["limit"],
        )
        return Response(EventSerializer(events, many=True).data)
