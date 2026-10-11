"""Thin HTTP layer for z_devices. Validation in serializers, logic in services."""
from __future__ import annotations

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from z_main.client import get_client_ip

from . import services
from .models import DeviceEvent
from .authentication import DeviceTokenAuthentication, IsDevice, IsOwner
from .serializers import (
    DevicePolicySerializer,
    DeviceSerializer,
    EnrollmentKeySerializer,
    EnrollSerializer,
    EventEmailSerializer,
    EventFeedQuerySerializer,
    EventSerializer,
    EventUploadSerializer,
    HeartbeatSerializer,
    MemberCreateSerializer,
    MemberReplySerializer,
    MemberSerializer,
    MessageCreateSerializer,
    MessageSerializer,
    MetricsQuerySerializer,
    MetricsTopQuerySerializer,
    MetricUploadSerializer,
    PolicySerializer,
    PolicyUpdateSerializer,
    WorkLogQuerySerializer,
    WorkLogSerializer,
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
            # The field name is the first (and only) key, which is how the agent
            # tells a refusal (member_id/email/non_field_errors) from a contract error.
            return Response({exc.field: [exc.message]}, status=status.HTTP_400_BAD_REQUEST)
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


class DeviceMessagesView(APIView):
    """
    `Authorization: Device <token>` -- the member's side of the chat.

    GET /api/devices/messages/  -> the conversation (both directions), newest first,
      as a BARE JSON array (the agent's contract); empty when the PC has no member.
    POST /api/devices/messages/ -> the member replies ({ "body": ... }); 201 with the new line.
    """

    authentication_classes = [DeviceTokenAuthentication]
    permission_classes = [IsDevice]

    def get(self, request: Request) -> Response:
        messages = services.messages_for_device(request.auth)
        return Response(MessageSerializer(messages, many=True).data)

    def post(self, request: Request) -> Response:
        serializer = MemberReplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        message = services.reply_from_member(request.auth, serializer.validated_data["body"])
        if message is None:
            return Response({"detail": "This PC isn't linked to a member."}, status=status.HTTP_400_BAD_REQUEST)
        return Response(MessageSerializer(message).data, status=status.HTTP_201_CREATED)


class DeviceMessagesReadView(APIView):
    """POST /api/devices/messages/read/ -- the agent marks the admin's messages seen on the member's PC."""

    authentication_classes = [DeviceTokenAuthentication]
    permission_classes = [IsDevice]

    def post(self, request: Request) -> Response:
        return Response({"marked": services.mark_read_by_member(request.auth)})


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


class MetricUploadView(APIView):
    """POST /api/devices/metrics/upload/ -- 1..1000 hour-bucketed usage rows (REPLACE)."""

    authentication_classes = [DeviceTokenAuthentication]
    permission_classes = [IsDevice]

    def post(self, request: Request) -> Response:
        serializer = MetricUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        accepted = services.store_metrics(request.auth, serializer.validated_data["metrics"])
        return Response({"accepted": accepted})


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


class MemberListCreateView(APIView):
    """GET/POST /api/devices/members/ -- list the owner's members, or register one."""

    permission_classes = [IsOwner]

    def get(self, request: Request) -> Response:
        members = services.members_for_owner(request.user)
        return Response(MemberSerializer(members, many=True).data)

    def post(self, request: Request) -> Response:
        serializer = MemberCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            member = services.create_member(
                request.user,
                name=serializer.validated_data["name"],
                email=serializer.validated_data["email"],
            )
        except services.MemberAlreadyExists as exc:
            return Response({"email": [str(exc)]}, status=status.HTTP_409_CONFLICT)
        return Response(MemberSerializer(member).data, status=status.HTTP_201_CREATED)


class MemberRotateIdView(APIView):
    """POST /api/devices/members/<id>/rotate-id/ -- mint a fresh enrol id."""

    permission_classes = [IsOwner]

    def post(self, request: Request, member_id: int) -> Response:
        member = get_object_or_404(request.user.members, pk=member_id)
        services.rotate_member_code(member)
        return Response(MemberSerializer(member).data)


class MemberDeactivateView(APIView):
    """POST /api/devices/members/<id>/deactivate/ -- switch off + revoke their PCs."""

    permission_classes = [IsOwner]

    def post(self, request: Request, member_id: int) -> Response:
        member = get_object_or_404(request.user.members, pk=member_id)
        services.deactivate_member(member)
        return Response(MemberSerializer(member).data)


class MemberMessageView(APIView):
    """POST /api/devices/members/<id>/message/ -- send one member a widget note."""

    permission_classes = [IsOwner]

    def post(self, request: Request, member_id: int) -> Response:
        member = get_object_or_404(request.user.members, pk=member_id)
        serializer = MessageCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        message = services.send_message(
            request.user,
            member,
            title=serializer.validated_data["title"],
            body=serializer.validated_data["body"],
            severity=serializer.validated_data["severity"],
            also_email=serializer.validated_data["also_email"],
        )
        return Response(MessageSerializer(message).data, status=status.HTTP_201_CREATED)


class MemberThreadView(APIView):
    """
    GET /api/devices/members/<id>/messages/ -- the full chat with one member (both
    directions), newest first. Viewing it marks the member's replies as seen.
    """

    permission_classes = [IsOwner]

    def get(self, request: Request, member_id: int) -> Response:
        member = get_object_or_404(request.user.members, pk=member_id)
        services.mark_thread_read_by_owner(member)
        thread = services.thread_for_owner(request.user, member)
        return Response(MessageSerializer(thread, many=True).data)


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
        events = list(events)
        emailed = services.alert_emails_sent(events)
        rows = EventSerializer(events, many=True).data
        for row in rows:
            row["emailed_at"] = emailed.get(row["id"])
        return Response(rows)


class EventEmailView(APIView):
    """
    POST /api/devices/events/<id>/email/ -- email the member whose PC raised this alert.
    Once per alert: 200 {sent_to, sent_at}; 409 {detail, sent_at} if already emailed;
    400 when the PC has no team member; 502 when the email couldn't be sent.
    """

    permission_classes = [IsOwner]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "alert_email"

    def post(self, request: Request, event_id: int) -> Response:
        event = get_object_or_404(
            DeviceEvent.objects.select_related("device__member"), pk=event_id, device__owner=request.user
        )
        serializer = EventEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            sent_to, sent_at = services.email_member_about_event(request.user, event, serializer.validated_data)
        except services.NoMemberForEvent:
            return Response(
                {"detail": "This PC isn't linked to a team member, so there's no one to email."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except services.AlreadyEmailed as exc:
            return Response(
                {"detail": "Already emailed.", "sent_at": exc.sent_at}, status=status.HTTP_409_CONFLICT
            )
        except services.EmailNotSent:
            return Response(
                {"detail": "We couldn't send the email just now. Try again in a minute."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response({"sent_to": sent_to, "sent_at": sent_at})


class WorkLogView(APIView):
    """
    GET /api/devices/work-log/?date=YYYY-MM-DD&member=<pk> -- when each member started,
    took breaks (with reasons) and ended their day. Own team only; date defaults to today.
    """

    permission_classes = [IsOwner]

    def get(self, request: Request) -> Response:
        query = WorkLogQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        day = query.validated_data.get("date") or timezone.localdate()
        logs = services.work_log_for_owner(request.user, day, query.validated_data.get("member"))
        return Response({"date": day, "entries": WorkLogSerializer(logs, many=True).data})


class MetricsFeedView(APIView):
    """
    GET /api/devices/metrics/?member=&device=&days=&category= -- usage for charts.

    Own devices only. Returns per-category totals and a per-bucket series.
    """

    permission_classes = [IsOwner]

    def get(self, request: Request) -> Response:
        query = MetricsQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = services.metrics_for_owner(
            request.user,
            member_id=query.validated_data.get("member"),
            device_id=query.validated_data.get("device"),
            days=query.validated_data["days"],
            category=query.validated_data.get("category"),
        )
        return Response(data)


class MetricsTopView(APIView):
    """
    GET /api/devices/metrics/top/?category=&days=&member=&device=&limit= -- the
    per-key breakdown within one category (top apps / top websites). Own devices only.
    """

    permission_classes = [IsOwner]

    def get(self, request: Request) -> Response:
        query = MetricsTopQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = services.top_metrics_for_owner(
            request.user,
            category=query.validated_data["category"],
            member_id=query.validated_data.get("member"),
            device_id=query.validated_data.get("device"),
            days=query.validated_data["days"],
            limit=query.validated_data["limit"],
        )
        return Response(data)
