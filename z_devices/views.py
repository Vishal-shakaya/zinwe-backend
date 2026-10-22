"""Thin HTTP layer for z_devices. Validation in serializers, logic in services."""
from __future__ import annotations

from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from z_auth import entitlements as plan
from z_main.client import get_client_ip

from . import services
from .models import AgentRelease, DeviceEvent, WorkLog
from .authentication import DeviceTokenAuthentication, IsDevice, IsOwner
from .serializers import (
    MemberFeedbackSerializer,
    NotificationPrefsSerializer,
    DevicePolicySerializer,
    DeviceSerializer,
    EnrollmentKeySerializer,
    EnrollSerializer,
    EventEmailSerializer,
    EventFeedQuerySerializer,
    EventHandledSerializer,
    EventSerializer,
    EventUploadSerializer,
    HeartbeatSerializer,
    MemberCreateSerializer,
    MemberReplySerializer,
    MemberRotateSerializer,
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
    BreakDecisionSerializer,
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
            if exc.pc_limit:  # the plan is full: tell the owner someone tried (rate-limited, J3-19)
                services.tell_owner_pc_refused(
                    exc, serializer.validated_data["email"], serializer.validated_data["hostname"]
                )
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
        owner = request.auth.owner
        policy = services.get_policy(owner)
        # Plan enforcement: the saved rules, clamped to what the owner's plan switches on, and to the
        # member's consent (declined = every monitoring feature off on this PC).
        data = services.policy_for_device(request.auth, DevicePolicySerializer(policy).data, policy)
        return Response(data)


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
        if not plan.entitlements_for(request.auth.owner).has(plan.TEAM_CHAT):
            return Response([])  # plan enforcement: no chat on this plan (bare array = agent contract)
        services.mark_delivered(request.auth)  # the PC has them now: "Delivered" for the admin
        messages = services.messages_for_device(request.auth)
        return Response(MessageSerializer(messages, many=True).data)

    def post(self, request: Request) -> Response:
        plan.require(request.auth.owner, plan.TEAM_CHAT)
        serializer = MemberReplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        message = services.reply_from_member(request.auth, serializer.validated_data["body"])
        if message is None:
            return Response({"detail": "This PC isn't linked to a member."}, status=status.HTTP_400_BAD_REQUEST)
        return Response(MessageSerializer(message).data, status=status.HTTP_201_CREATED)


class OwnerFeedView(APIView):
    """
    GET /api/devices/notifications/?since=<iso> -- what's new for the owner since then (FR-15): {now, items}. The
    dashboard polls it and shows browser notifications; pass back `now` as the next `since`. No `since` = no items.
    """

    permission_classes = [IsOwner]

    def get(self, request: Request) -> Response:
        from django.utils.dateparse import parse_datetime

        from .owner_feed import feed

        raw = request.query_params.get("since", "")
        since = parse_datetime(raw) if raw else None
        if raw and since is None:
            return Response({"since": ["Use an ISO date and time."]}, status=status.HTTP_400_BAD_REQUEST)
        if since is not None and timezone.is_naive(since):
            since = timezone.make_aware(since)
        return Response(feed(request.user, since))


class NotificationPrefsView(APIView):
    """GET/PATCH /api/devices/notifications/settings/ -- {chat, alerts: all|warning|critical|off, approvals}."""

    permission_classes = [IsOwner]

    def get(self, request: Request) -> Response:
        from .owner_feed import prefs_for

        return Response(NotificationPrefsSerializer(prefs_for(request.user)).data)

    def patch(self, request: Request) -> Response:
        from .owner_feed import prefs_for

        serializer = NotificationPrefsSerializer(prefs_for(request.user), data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class DeviceFeedbackView(APIView):
    """
    POST /api/devices/feedback/ (Device auth) -- {rating 1-10, message?} from the member's Zinwe window (FR-14).
    It goes to the Zinwe team (admin app, Feedback), filed under the company it came from; the owner doesn't see it.
    """

    authentication_classes = [DeviceTokenAuthentication]
    permission_classes = [IsDevice]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "member_feedback"

    def post(self, request: Request) -> Response:
        from z_auth.models import Feedback

        serializer = MemberFeedbackSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        device = request.auth
        feedback = Feedback.objects.create(
            owner=device.owner,
            source=Feedback.Source.MEMBER,
            member=device.member,
            device=device,
            rating=serializer.validated_data["rating"],
            message=serializer.validated_data.get("message", ""),
            page="Zinwe window",
        )
        return Response({"id": feedback.pk}, status=status.HTTP_201_CREATED)


class DeviceMessagesReadView(APIView):
    """POST /api/devices/messages/read/ -- the agent reports the member opened the admin's messages."""

    authentication_classes = [DeviceTokenAuthentication]
    permission_classes = [IsDevice]

    def post(self, request: Request) -> Response:
        return Response({"marked": services.mark_read_by_member(request.auth)})


class DeviceReleaseDownloadView(APIView):
    """
    GET /api/devices/releases/<id>/download/ (Device auth) -- the release's MSI, for the agent's
    one-click update. Only enrolled PCs can fetch it; the agent checks the SHA-256 from the heartbeat.
    """

    authentication_classes = [DeviceTokenAuthentication]
    permission_classes = [IsDevice]

    def get(self, request: Request, pk: int) -> FileResponse:
        release = get_object_or_404(AgentRelease, pk=pk, is_active=True)
        if not release.file:
            raise Http404("This release is hosted elsewhere.")
        return FileResponse(
            release.file.open("rb"), as_attachment=True,
            filename=f"ZinweAgent-{release.version}.msi", content_type="application/x-msi",
        )


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
    """
    POST /api/devices/members/<id>/rotate/ (also rotate-id/) -- mint a fresh enrol id. Body {email: bool},
    default true: the member is emailed the new ID (their old invite stops working).
    """

    permission_classes = [IsOwner]

    def post(self, request: Request, member_id: int) -> Response:
        member = get_object_or_404(request.user.members, pk=member_id)
        data = MemberRotateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        services.rotate_member_code(member, email=data.validated_data["email"])
        return Response(MemberSerializer(member).data)


class DeviceUsbRestoreView(APIView):
    """
    POST /api/devices/<id>/usb/restore/ -- the owner gives a USB-locked PC its USB back: the next heartbeat
    tells the agent to go back to the policy (it then sends usb.restored). Returns the device.
    """

    permission_classes = [IsOwner]

    def post(self, request: Request, device_id: int) -> Response:
        device = get_object_or_404(request.user.devices, pk=device_id)
        if device.is_revoked:
            return Response({"detail": "This PC was removed from your team."}, status=status.HTTP_409_CONFLICT)
        services.request_usb_restore(request.user, device)
        return Response(DeviceSerializer(device).data)


class MemberDeactivateView(APIView):
    """POST /api/devices/members/<id>/deactivate/ -- switch off + revoke their PCs."""

    permission_classes = [IsOwner]

    def post(self, request: Request, member_id: int) -> Response:
        member = get_object_or_404(request.user.members, pk=member_id)
        services.deactivate_member(member)
        return Response(MemberSerializer(member).data)


class MemberMessageView(APIView):
    """POST /api/devices/members/<id>/message/ -- send one member a note (widget, widget + email, or email)."""

    permission_classes = [IsOwner]

    def post(self, request: Request, member_id: int) -> Response:
        plan.require(request.user, plan.TEAM_CHAT)
        member = get_object_or_404(request.user.members, pk=member_id)
        serializer = MessageCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        event = None
        event_id = serializer.validated_data.get("event_id")
        if event_id:
            event = DeviceEvent.objects.filter(pk=event_id, device__owner=request.user).first()
            if event is None:
                return Response({"event_id": ["That alert wasn't found."]}, status=status.HTTP_400_BAD_REQUEST)
        message = services.send_message(
            request.user,
            member,
            title=serializer.validated_data["title"],
            body=serializer.validated_data["body"],
            severity=serializer.validated_data["severity"],
            channel=serializer.validated_data["channel"],
            event=event,
        )
        return Response(MessageSerializer(message).data, status=status.HTTP_201_CREATED)


class MemberThreadView(APIView):
    """
    GET /api/devices/members/<id>/messages/ -- the full chat with one member (both
    directions), newest first. Viewing it marks the member's replies as seen.
    """

    permission_classes = [IsOwner]

    def get(self, request: Request, member_id: int) -> Response:
        plan.require(request.user, plan.TEAM_CHAT)
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
    """
    GET /api/devices/events/?device=<id>&member=<id>&types=<a,b>&limit=<1..200> -- newest first, own PCs only.
    """

    permission_classes = [IsOwner]

    def get(self, request: Request) -> Response:
        query = EventFeedQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        events = services.events_for_owner(
            request.user,
            device_id=query.validated_data.get("device"),
            limit=query.validated_data["limit"],
            member_id=query.validated_data.get("member"),
            types=query.validated_data.get("types"),
            kind=query.validated_data.get("kind"),
            handled=query.validated_data.get("handled"),
        )
        events = list(events)
        emailed = services.alert_emails_sent(events)
        rows = EventSerializer(events, many=True).data
        for row in rows:
            row["emailed_at"] = emailed.get(row["id"])
        return Response(rows)


class EventCountsView(APIView):
    """GET /api/devices/events/counts/ -- {alerts, unhandled, critical_unhandled}; rule confirmations excluded."""

    permission_classes = [IsOwner]

    def get(self, request: Request) -> Response:
        return Response(services.alert_counts(request.user))


class EventHandledView(APIView):
    """POST /api/devices/events/<id>/handled/ {handled: bool} -- mark an alert dealt with (or not). Returns it."""

    permission_classes = [IsOwner]

    def post(self, request: Request, event_id: int) -> Response:
        event = get_object_or_404(DeviceEvent.objects.select_related("device"), pk=event_id, device__owner=request.user)
        data = EventHandledSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        event = services.mark_event_handled(request.user, event, data.validated_data["handled"])
        row = EventSerializer(event).data
        row["emailed_at"] = services.alert_emails_sent([event]).get(event.pk)
        return Response(row)


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
        plan.require(request.user, plan.ALERT_TO_EMPLOYEE)
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
        plan.require(request.user, plan.WORK_TIME)
        query = WorkLogQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        day = query.validated_data.get("date") or timezone.localdate()
        logs = services.work_log_for_owner(request.user, day, query.validated_data.get("member"))
        return Response({"date": day, "entries": WorkLogSerializer(logs, many=True).data})


class BreakDecisionView(APIView):
    """POST /api/devices/work-log/<id>/decision/ {decision: approved|denied, note?} -- owner's call on a break."""

    permission_classes = [IsOwner]

    def post(self, request: Request, log_id: int) -> Response:
        plan.require(request.user, plan.WORK_TIME)
        log = get_object_or_404(WorkLog.objects.select_related("member", "device"), pk=log_id, owner=request.user)
        data = BreakDecisionSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            log = services.decide_break(request.user, log, data.validated_data["decision"], data.validated_data["note"])
        except services.NotABreak:
            return Response({"detail": "Only breaks can be approved or denied."}, status=status.HTTP_400_BAD_REQUEST)
        return Response(WorkLogSerializer(log).data)


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
