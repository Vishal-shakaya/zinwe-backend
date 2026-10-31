"""Thin HTTP layer for z_devices. Validation in serializers, logic in services."""
from __future__ import annotations

import re

from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.parsers import JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from z_auth import entitlements as plan
from z_main.client import get_client_ip

from . import member_todos, pictures, services, work_tasks
from .models import AgentRelease, DeviceEvent, WorkLog
from .authentication import DeviceScopedThrottle, DeviceTokenAuthentication, IsDevice, IsOwner
from .serializers import (
    MemberFeedbackSerializer,
    MemberUpdateSerializer,
    NotificationPrefsSerializer,
    DevicePolicySerializer,
    DeviceSerializer,
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
    """POST /api/devices/enroll/ -- exchange the member id + email for a device token (no org key since v1)."""

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
    """POST /api/devices/heartbeat/ -- `Authorization: Device <token>`. Throttled per PC (FIX-006)."""

    authentication_classes = [DeviceTokenAuthentication]
    permission_classes = [IsDevice]
    throttle_classes = [DeviceScopedThrottle]
    throttle_scope = "device_heartbeat"

    def post(self, request: Request) -> Response:
        serializer = HeartbeatSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        response = services.record_heartbeat(request.auth, serializer.validated_data, get_client_ip(request))
        if request.auth.member_id:  # FR-44: the tray keeps its member's picture in step (null = initials)
            response["picture"] = pictures.picture_payload(request.auth.member, request)
        return Response(response)


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
        return Response(MessageSerializer(messages, many=True, context={"request": request}).data)

    def post(self, request: Request) -> Response:
        plan.require(request.auth.owner, plan.TEAM_CHAT)
        serializer = MemberReplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        message = services.reply_from_member(request.auth, serializer.validated_data["body"])
        if message is None:
            return Response({"detail": "This PC isn't linked to a member."}, status=status.HTTP_400_BAD_REQUEST)
        return Response(MessageSerializer(message, context={"request": request}).data, status=status.HTTP_201_CREATED)


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
    """POST /api/devices/events/upload/ -- 1..100 queued events, idempotent on `id`. Throttled per PC."""

    authentication_classes = [DeviceTokenAuthentication]
    permission_classes = [IsDevice]
    throttle_classes = [DeviceScopedThrottle]
    throttle_scope = "device_events"

    def post(self, request: Request) -> Response:
        serializer = EventUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        accepted, duplicates = services.store_events(
            request.auth, serializer.validated_data["events"]
        )
        return Response({"accepted": accepted, "duplicates": duplicates})


class MetricUploadView(APIView):
    """POST /api/devices/metrics/upload/ -- 1..1000 hour-bucketed usage rows (REPLACE). Throttled per PC."""

    authentication_classes = [DeviceTokenAuthentication]
    permission_classes = [IsDevice]
    throttle_classes = [DeviceScopedThrottle]
    throttle_scope = "device_metrics"

    def post(self, request: Request) -> Response:
        serializer = MetricUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        accepted = services.store_metrics(request.auth, serializer.validated_data["metrics"])
        return Response({"accepted": accepted})


# --- Owner endpoints (called by the dashboard) ------------------------------


ENROLLMENT_KEY_GONE = (
    "Enrolment keys are retired. A PC joins with its member's id and email: add the person on the "
    "Team page and send them their member id."
)


class EnrollmentKeyView(APIView):
    """GET /api/devices/enrollment-key/ -- RETIRED (FIX-005): 410 Gone. Enrolment uses the member id + email."""

    permission_classes = [IsOwner]

    def get(self, request: Request) -> Response:
        return Response({"detail": ENROLLMENT_KEY_GONE}, status=status.HTTP_410_GONE)


class EnrollmentKeyRotateView(APIView):
    """POST /api/devices/enrollment-key/rotate/ -- RETIRED (FIX-005): 410 Gone, nothing is changed."""

    permission_classes = [IsOwner]

    def post(self, request: Request) -> Response:
        return Response({"detail": ENROLLMENT_KEY_GONE}, status=status.HTTP_410_GONE)


class DeviceListView(APIView):
    """
    GET /api/devices/ -- the owner's PCs in use, with online status (FIX-005: removed PCs left out).
    `?include_revoked=1` adds the removed ones (`is_revoked: true`), for history views that name old alerts' PCs.
    """

    permission_classes = [IsOwner]

    def get(self, request: Request) -> Response:
        devices = request.user.devices.all()
        if request.query_params.get("include_revoked") not in ("1", "true", "yes"):
            devices = devices.filter(is_revoked=False)
        return Response(DeviceSerializer(devices, many=True).data)


class MemberListCreateView(APIView):
    """GET/POST /api/devices/members/ -- list the owner's members, or register one."""

    permission_classes = [IsOwner]

    def get(self, request: Request) -> Response:
        members = services.members_for_owner(request.user)
        return Response(MemberSerializer(members, many=True, context={"request": request}).data)

    def post(self, request: Request) -> Response:
        serializer = MemberCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            member = services.create_member(
                request.user,
                name=serializer.validated_data["name"],
                email=serializer.validated_data["email"],
                role=serializer.validated_data.get("role", ""),
            )
        except services.MemberAlreadyExists as exc:
            return Response({"email": [str(exc)]}, status=status.HTTP_409_CONFLICT)
        return Response(MemberSerializer(member, context={"request": request}).data, status=status.HTTP_201_CREATED)


class MemberDetailView(APIView):
    """PATCH /api/devices/members/<id>/ {role} -- what this member does (FR-19: their card's colour theme)."""

    permission_classes = [IsOwner]

    def patch(self, request: Request, member_id: int) -> Response:
        from .models import Member

        member = get_object_or_404(Member, pk=member_id, owner=request.user)
        data = MemberUpdateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        member.role = data.validated_data["role"]
        member.save(update_fields=["role"])
        return Response(MemberSerializer(member, context={"request": request}).data)


class MemberTagsView(APIView):
    """GET /api/devices/members/tags/ -- playful, kind tags per member from the last 7 days (FR-19), owner only."""

    permission_classes = [IsOwner]

    def get(self, request: Request) -> Response:
        from .tags import tags_for_owner

        return Response(tags_for_owner(request.user))


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
        return Response(MemberSerializer(member, context={"request": request}).data)


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
        return Response(MemberSerializer(member, context={"request": request}).data)


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
        return Response(MessageSerializer(message, context={"request": request}).data, status=status.HTTP_201_CREATED)


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
        return Response(MessageSerializer(thread, many=True, context={"request": request}).data)


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
    FIX-011: also `carried_in` (yesterday's changes still running at midnight) and per-person `totals`
    for the day, split at midnight the same way reports are.
    """

    permission_classes = [IsOwner]

    def get(self, request: Request) -> Response:
        from .work_time import work_zone

        plan.require(request.user, plan.WORK_TIME)
        query = WorkLogQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        day = query.validated_data.get("date") or timezone.localdate(timezone=work_zone(request.user))
        member_id = query.validated_data.get("member")
        logs = services.work_log_for_owner(request.user, day, member_id)
        day_totals = services.work_day_totals(request.user, day, member_id)
        return Response({
            "date": day,
            "entries": WorkLogSerializer(logs, many=True).data,
            "carried_in": WorkLogSerializer(day_totals["carried_in"], many=True).data,
            "totals": day_totals["totals"],
        })


def _report_member(request: Request):  # type: ignore[no-untyped-def]
    """The ?member=<id> a report is about (one of the owner's own), or None for the whole team."""
    from .models import Member

    raw = request.query_params.get("member", "")
    if not raw:
        return None
    if not raw.isdigit():
        raise Http404
    return get_object_or_404(Member, pk=int(raw), owner=request.user)


class ReportView(APIView):
    """
    GET /api/devices/reports/?from=YYYY-MM-DD&to=YYYY-MM-DD[&member=<id>] -- the report for that period (FR-17,
    FR-22), for the team or one person: per person days worked, time worked / on breaks, breaks, log-outs, waiting
    for OK, usual start and finish, active time, internet data, alerts; the day-by-day trend; top apps, sites and
    data use; for one person, each of their days.
    GET /api/devices/reports/download/?from=&to=[&member=][&file=csv|pdf] -- the same as a CSV or (paid plans,
    reports_pro) PDF file.
    """

    permission_classes = [IsOwner]
    download = False

    def get(self, request: Request) -> Response | HttpResponse:
        from django.utils.dateparse import parse_date

        from .report_mail import company_name
        from .report_pdf import report_filename, report_pdf
        from .reports import BadPeriod, build_report, clamp_period, report_csv

        raw_from, raw_to = request.query_params.get("from", ""), request.query_params.get("to", "")
        try:
            start, end = parse_date(raw_from), parse_date(raw_to)
        except ValueError:
            start = end = None
        if start is None or end is None:
            return Response({"detail": "Choose the period's first and last day (YYYY-MM-DD)."},
                            status=status.HTTP_400_BAD_REQUEST)
        member = _report_member(request)
        fmt = request.query_params.get("file", "csv") if self.download else "json"  # not "format": DRF owns that
        if fmt == "pdf":
            plan.require(request.user, plan.REPORTS_PRO)
        try:
            start, end = clamp_period(request.user, start, end)
        except BadPeriod as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        report = build_report(request.user, start, end, member=member)
        if not self.download:
            return Response(report)
        company = company_name(request.user)
        if fmt == "pdf":
            response = HttpResponse(report_pdf(report, company), content_type="application/pdf")
            response["Content-Disposition"] = f'attachment; filename="{report_filename(report)}"'
            return response
        response = HttpResponse(report_csv(report, company), content_type="text/csv; charset=utf-8")
        name = f"Zinwe-report-{start}-to-{end}.csv" if member is None else report_filename(report, "csv")
        response["Content-Disposition"] = f'attachment; filename="{name}"'
        return response


class ReportDownloadView(ReportView):
    download = True


class ReportSendView(APIView):
    """
    POST /api/devices/reports/send/ {from, to, member?, send_to_owner, send_to_members, emails[], include_csv}
    -- email the report now (FR-22, reports_pro). 200 = the recorded delivery; 429 after 20 sends in a day.
    """

    permission_classes = [IsOwner]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "report_send"

    def post(self, request: Request) -> Response:
        from .report_mail import MAX_MANUAL_SENDS_PER_DAY, manual_sends_today, send_report
        from .reports import BadPeriod
        from .serializers import ReportDeliverySerializer, ReportSendSerializer

        plan.require(request.user, plan.REPORTS_PRO)
        data = ReportSendSerializer(data=request.data, context={"owner": request.user})
        data.is_valid(raise_exception=True)
        if manual_sends_today(request.user) >= MAX_MANUAL_SENDS_PER_DAY:
            return Response({"detail": "That's 20 reports emailed today. Try again tomorrow, or download it."},
                            status=status.HTTP_429_TOO_MANY_REQUESTS)
        v = data.validated_data
        try:
            delivery = send_report(
                request.user, v["start"], v["end"], member=v.get("member"), to_owner=v["send_to_owner"],
                to_members=v["send_to_members"], emails=v["emails"], include_csv=v["include_csv"],
            )
        except BadPeriod as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ReportDeliverySerializer(delivery).data)


class ReportScheduleListView(APIView):
    """GET/POST /api/devices/reports/schedules/ -- the owner's scheduled reports (FR-22; creating needs reports_pro)."""

    permission_classes = [IsOwner]

    def get(self, request: Request) -> Response:
        from .models import ReportSchedule
        from .serializers import ReportScheduleSerializer

        schedules = ReportSchedule.objects.filter(owner=request.user).select_related("member")
        return Response(ReportScheduleSerializer(schedules, many=True, context={"owner": request.user}).data)

    def post(self, request: Request) -> Response:
        from .models import ReportSchedule
        from .report_schedules import MAX_SCHEDULES, schedule_first_run
        from .serializers import ReportScheduleSerializer

        plan.require(request.user, plan.REPORTS_PRO)
        if ReportSchedule.objects.filter(owner=request.user).count() >= MAX_SCHEDULES:
            return Response({"detail": f"You can have up to {MAX_SCHEDULES} scheduled reports."},
                            status=status.HTTP_400_BAD_REQUEST)
        data = ReportScheduleSerializer(data=request.data, context={"owner": request.user})
        data.is_valid(raise_exception=True)
        schedule = ReportSchedule(owner=request.user, **data.validated_data)
        schedule_first_run(schedule)
        schedule.save()
        return Response(ReportScheduleSerializer(schedule, context={"owner": request.user}).data,
                        status=status.HTTP_201_CREATED)


class ReportScheduleDetailView(APIView):
    """PATCH/DELETE /api/devices/reports/schedules/<id>/ -- change (reports_pro), pause or remove a schedule."""

    permission_classes = [IsOwner]

    def patch(self, request: Request, schedule_id: int) -> Response:
        from .models import ReportSchedule
        from .report_schedules import schedule_first_run
        from .serializers import ReportScheduleSerializer

        schedule = get_object_or_404(ReportSchedule, pk=schedule_id, owner=request.user)
        if set(request.data) - {"is_active"} or request.data.get("is_active"):
            plan.require(request.user, plan.REPORTS_PRO)  # pausing is always allowed
        data = ReportScheduleSerializer(schedule, data=request.data, partial=True, context={"owner": request.user})
        data.is_valid(raise_exception=True)
        timing = {"frequency", "weekday", "month_day", "every_days", "send_hour", "is_active"}
        schedule = data.save()
        if timing & set(data.validated_data):
            schedule_first_run(schedule)
            schedule.save(update_fields=["next_run_at"])
        return Response(ReportScheduleSerializer(schedule, context={"owner": request.user}).data)

    def delete(self, request: Request, schedule_id: int) -> Response:
        from .models import ReportSchedule

        get_object_or_404(ReportSchedule, pk=schedule_id, owner=request.user).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ReportScheduleSendNowView(APIView):
    """POST /api/devices/reports/schedules/<id>/send-now/ -- send a schedule's latest period now (to try it)."""

    permission_classes = [IsOwner]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "report_send"

    def post(self, request: Request, schedule_id: int) -> Response:
        from .models import ReportSchedule
        from .report_mail import MAX_MANUAL_SENDS_PER_DAY, manual_sends_today, send_report
        from .report_schedules import period_for
        from .serializers import ReportDeliverySerializer

        plan.require(request.user, plan.REPORTS_PRO)
        schedule = get_object_or_404(ReportSchedule.objects.select_related("member"), pk=schedule_id, owner=request.user)
        if manual_sends_today(request.user) >= MAX_MANUAL_SENDS_PER_DAY:
            return Response({"detail": "That's 20 reports emailed today. Try again tomorrow."},
                            status=status.HTTP_429_TOO_MANY_REQUESTS)
        start, end = period_for(schedule, timezone.localdate())
        delivery = send_report(
            request.user, start, end, member=schedule.member, to_owner=schedule.send_to_owner,
            to_members=schedule.send_to_members, emails=schedule.extra_emails, include_csv=schedule.include_csv,
        )
        ReportSchedule.objects.filter(pk=schedule.pk).update(last_status=f"Sent now: {delivery.detail}"[:200])
        return Response(ReportDeliverySerializer(delivery).data)


class ReportDeliveryListView(APIView):
    """GET /api/devices/reports/deliveries/ -- the last 50 reports emailed (FR-22's "Sent" list)."""

    permission_classes = [IsOwner]

    def get(self, request: Request) -> Response:
        from .models import ReportDelivery
        from .serializers import ReportDeliverySerializer

        rows = ReportDelivery.objects.filter(owner=request.user).select_related("member")[:50]
        return Response(ReportDeliverySerializer(rows, many=True).data)


class BreakDecisionView(APIView):
    """POST /api/devices/work-log/<id>/decision/ {decision: approved|denied, note?} -- owner's call on a break or
    log-out. `note` is at most 280 characters (FIX-012); longer is a 400 that says so."""

    permission_classes = [IsOwner]

    def post(self, request: Request, log_id: int) -> Response:
        plan.require(request.user, plan.WORK_TIME)
        log = get_object_or_404(WorkLog.objects.select_related("member", "device"), pk=log_id, owner=request.user)
        data = BreakDecisionSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            log = services.decide_break(request.user, log, data.validated_data["decision"], data.validated_data["note"])
        except services.NotABreak:
            return Response({"detail": "Only breaks and log-outs can be approved or denied."}, status=status.HTTP_400_BAD_REQUEST)
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


# --- FR-20 v2: tasks (owner: token auth; the member's PC: Device token) ------------------------------------


class IsOwnerOrDevice(IsOwner):
    """A logged-in owner, or an enrolled PC (each view then branches on which)."""

    def has_permission(self, request: Request, view) -> bool:  # type: ignore[no-untyped-def]
        return IsDevice().has_permission(request, view) or super().has_permission(request, view)


def _is_device(request: Request) -> bool:
    from .models import Device

    return isinstance(request.auth, Device)


# The Windows app's id for a to-do (a guid without dashes); anything else never reaches the database.
TODO_CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")


class _TaskView(APIView):
    """Owner or device. Device calls are throttled per PC; owner calls aren't."""

    authentication_classes = [DeviceTokenAuthentication, *APIView.authentication_classes]
    permission_classes = [IsOwnerOrDevice]
    throttle_scope = "device_tasks"

    def get_throttles(self):  # type: ignore[no-untyped-def]
        return [DeviceScopedThrottle()] if _is_device(self.request) else []


def _refused(exc: "work_tasks.TaskRefused") -> Response:
    return Response({exc.field: [exc.message]}, status=status.HTTP_400_BAD_REQUEST)


class TaskListView(_TaskView):
    """
    GET /api/devices/tasks/
    - owner: ?status=open|done|all (default open; done = approved or cancelled) &member=<id> -> [Task], newest
      activity first.
    - device: the PC's member's open tasks + the ones approved in the last 7 days -> [Task] (bare array; empty when
      the plan has no team_chat or the PC has no member).
    """

    def get(self, request: Request) -> Response:
        from .serializers import TaskListQuerySerializer, TaskSerializer

        if _is_device(request):
            if not plan.entitlements_for(request.auth.owner).has(plan.TEAM_CHAT):
                return Response([])
            return Response(TaskSerializer(work_tasks.tasks_for_device(request.auth), many=True, context={"request": request}).data)
        plan.require(request.user, plan.TEAM_CHAT)
        query = TaskListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        tasks = work_tasks.tasks_for_owner(request.user, query.validated_data["status"], query.validated_data["member"])
        return Response(TaskSerializer(tasks, many=True, context={"request": request}).data)


class MemberTaskCreateView(APIView):
    """POST /api/devices/members/<id>/tasks/ -- {title, note?, due_at?, priority?, steps?: [title]} -> Task (201)."""

    permission_classes = [IsOwner]

    def post(self, request: Request, member_id: int) -> Response:
        from .serializers import TaskCreateSerializer, TaskSerializer

        plan.require(request.user, plan.TEAM_CHAT)
        member = get_object_or_404(request.user.members, pk=member_id)
        serializer = TaskCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            task = work_tasks.create_task(request.user, member, **serializer.validated_data)
        except work_tasks.TaskRefused as exc:
            return _refused(exc)
        return Response(TaskSerializer(task, context={"request": request}).data, status=status.HTTP_201_CREATED)


class TaskDetailView(APIView):
    """PATCH /api/devices/tasks/<id>/ -- {title?, note?, due_at?, priority?, steps?} -> Task (owner only)."""

    permission_classes = [IsOwner]

    def patch(self, request: Request, task_id: int) -> Response:
        from .serializers import TaskSerializer, TaskUpdateSerializer

        plan.require(request.user, plan.TEAM_CHAT)
        task = work_tasks.task_for_owner(request.user, task_id)
        if task is None:
            raise Http404
        serializer = TaskUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        try:
            task = work_tasks.edit_task(request.user, task, serializer.validated_data)
        except work_tasks.TaskRefused as exc:
            return _refused(exc)
        return Response(TaskSerializer(task, context={"request": request}).data)


class TaskActionView(_TaskView):
    """
    POST /api/devices/tasks/<id>/action/ -> Task
    - owner: {action: approve|reopen|cancel|nudge, note?}
    - device (the task's own member only): {action: seen|accept|start|help|done|note, note?, step_id?, step_done?}
    """

    def post(self, request: Request, task_id: int) -> Response:
        from .serializers import TaskDeviceActionSerializer, TaskOwnerActionSerializer, TaskSerializer

        device = _is_device(request)
        plan.require(request.auth.owner if device else request.user, plan.TEAM_CHAT)
        task = (
            work_tasks.task_for_device(request.auth, task_id) if device else work_tasks.task_for_owner(request.user, task_id)
        )
        if task is None:
            raise Http404
        serializer = (TaskDeviceActionSerializer if device else TaskOwnerActionSerializer)(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            if device:
                task = work_tasks.member_action(
                    request.auth, task, data["action"], data["note"], data["step_id"], data["step_done"]
                )
            else:
                task = work_tasks.owner_action(request.user, task, data["action"], data["note"])
        except work_tasks.TaskRefused as exc:
            return _refused(exc)
        return Response(TaskSerializer(task, context={"request": request}).data)


# --- A member's own to-dos (the member's PC writes, the owner only reads) -----------------------------------


class TodoListView(_TaskView):
    """
    GET /api/devices/todos/ -- same plan module as tasks (team_chat); without it, 403 and the Windows app stays local.
    - owner: ?status=open|done|all (default all) &member=<id> -> [Todo], newest change first; deleted ones left out.
    - device: {member_id, todos: [Todo]}: the PC's member's to-dos, with `deleted` ones, so their other PCs drop them.
    """

    def get(self, request: Request) -> Response:
        from .serializers import MemberTodoListQuerySerializer, MemberTodoSerializer

        if _is_device(request):
            plan.require(request.auth.owner, plan.TEAM_CHAT)
            todos = member_todos.todos_for_device(request.auth)
            return Response({"member_id": request.auth.member_id, "todos": MemberTodoSerializer(todos, many=True).data})
        plan.require(request.user, plan.TEAM_CHAT)
        query = MemberTodoListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        todos = member_todos.todos_for_owner(request.user, query.validated_data["status"], query.validated_data["member"])
        return Response(MemberTodoSerializer(todos, many=True).data)


class MemberTodosView(APIView):
    """GET /api/devices/members/<id>/todos/ -- ?status=open|done|all (default all) -> [Todo] (owner, read-only)."""

    permission_classes = [IsOwner]

    def get(self, request: Request, member_id: int) -> Response:
        from .serializers import MemberTodoListQuerySerializer, MemberTodoSerializer

        plan.require(request.user, plan.TEAM_CHAT)
        member = get_object_or_404(request.user.members, pk=member_id)
        query = MemberTodoListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        todos = member_todos.todos_for_owner(request.user, query.validated_data["status"], member.pk)
        return Response(MemberTodoSerializer(todos, many=True).data)


class DeviceTodoView(APIView):
    """
    `Authorization: Device <token>` -- the member's own to-do, from their Zinwe window. Safe to send twice or late.
    PUT /api/devices/todos/<client_id>/ -- {title, due_date?, steps?: [{title, done?}], status?, updated_at} -> Todo
    (201 when new). DELETE -- {updated_at} -> Todo. Either way the answer is the row as it stands: a write older than
    the row changes nothing. 409 when the PC isn't linked to a member.
    """

    authentication_classes = [DeviceTokenAuthentication]
    permission_classes = [IsDevice]
    throttle_classes = [DeviceScopedThrottle]
    throttle_scope = "device_tasks"

    def _refused(self, request: Request, client_id: str) -> Response | None:
        plan.require(request.auth.owner, plan.TEAM_CHAT)
        if not TODO_CLIENT_ID.fullmatch(client_id):
            return Response({"client_id": ["Not a to-do id."]}, status=status.HTTP_400_BAD_REQUEST)
        if request.auth.member_id is None:
            return Response(
                {"code": "no_member", "detail": "This PC isn't linked to a member."}, status=status.HTTP_409_CONFLICT
            )
        return None

    def put(self, request: Request, client_id: str) -> Response:
        from .serializers import MemberTodoSaveSerializer, MemberTodoSerializer

        if (refused := self._refused(request, client_id)) is not None:
            return refused
        serializer = MemberTodoSaveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        todo, created = member_todos.save_todo(request.auth, client_id, serializer.validated_data)
        return Response(
            MemberTodoSerializer(todo).data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK
        )

    def delete(self, request: Request, client_id: str) -> Response:
        from .serializers import MemberTodoDeleteSerializer, MemberTodoSerializer

        if (refused := self._refused(request, client_id)) is not None:
            return refused
        serializer = MemberTodoDeleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        todo = member_todos.delete_todo(request.auth, client_id, serializer.validated_data["updated_at"])
        return Response(MemberTodoSerializer(todo).data)


# --- FR-44: team pictures -----------------------------------------------------------------------------------


def _picture_error(exc: "pictures.PictureRejected") -> Response:
    return Response({"code": exc.code, "detail": exc.detail}, status=status.HTTP_400_BAD_REQUEST)


def _apply_picture(request: Request, member) -> Response:  # type: ignore[no-untyped-def]
    """PUT body: JSON {kind: "avatar", key} or multipart `file` (a photo). Shared by the owner and device views."""
    try:
        upload = request.FILES.get("file")
        if upload is not None:
            if upload.size > pictures.MAX_UPLOAD_BYTES:  # refused before reading it
                raise pictures.PictureRejected("too_large", "That photo is too big. Pick one under 10 MB and try again.")
            pictures.set_photo(member, upload.read())
        elif request.data.get("kind") == "avatar":
            pictures.set_avatar(member, str(request.data.get("key") or ""))
        elif request.content_type.startswith("multipart/"):
            raise pictures.PictureRejected("not_an_image", "Choose a photo to upload.")
        else:
            return Response(
                {"code": "bad_request", "detail": 'Send {"kind": "avatar", "key": ...} or a photo as `file`.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
    except pictures.PictureRejected as exc:
        return _picture_error(exc)
    return Response({"picture": pictures.picture_payload(member, request)})


class MemberPictureView(APIView):
    """
    PUT /api/devices/members/<id>/picture/ -- JSON {kind: avatar, key} or multipart `file` -> 200 {picture}.
    DELETE -> 204 (back to initials). Owner only, their own members only. 30 writes/hour per owner.
    """

    permission_classes = [IsOwner]
    parser_classes = [JSONParser, MultiPartParser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "member_picture"

    def put(self, request: Request, member_id: int) -> Response:
        member = get_object_or_404(request.user.members, pk=member_id)
        return _apply_picture(request, member)

    def delete(self, request: Request, member_id: int) -> Response:
        member = get_object_or_404(request.user.members, pk=member_id)
        pictures.clear(member)
        return Response(status=status.HTTP_204_NO_CONTENT)


class DevicePictureView(APIView):
    """
    `Authorization: Device <token>` -- the member's own picture, from their Zinwe window (tray -> Me).
    GET -> {picture}; PUT (same bodies as the owner's) -> 200 {picture}; DELETE -> 204. Writes: 30/hour per PC.
    409 when the PC isn't linked to a member.
    """

    authentication_classes = [DeviceTokenAuthentication]
    permission_classes = [IsDevice]
    parser_classes = [JSONParser, MultiPartParser]
    throttle_scope = "device_picture"

    def get_throttles(self):  # type: ignore[no-untyped-def]
        return [] if self.request.method in ("GET", "HEAD", "OPTIONS") else [DeviceScopedThrottle()]

    def _member(self, request: Request):  # type: ignore[no-untyped-def]
        return request.auth.member if request.auth.member_id else None

    def _unlinked(self) -> Response:
        return Response(
            {"code": "no_member", "detail": "This PC isn't linked to a member."}, status=status.HTTP_409_CONFLICT
        )

    def get(self, request: Request) -> Response:
        member = self._member(request)
        if member is None:
            return self._unlinked()
        return Response({"picture": pictures.picture_payload(member, request)})

    def put(self, request: Request) -> Response:
        member = self._member(request)
        if member is None:
            return self._unlinked()
        return _apply_picture(request, member)

    def delete(self, request: Request) -> Response:
        member = self._member(request)
        if member is None:
            return self._unlinked()
        pictures.clear(member)
        return Response(status=status.HTTP_204_NO_CONTENT)


class PictureFileView(APIView):
    """
    GET /api/devices/pictures/<signed>.jpg -- a member's photo. No auth header (works in <img> and the tray): the
    signed token (member id + picture version) is the key. Changed / removed picture or deleted member -> 404.
    Cached for a year: the URL changes whenever the picture does.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request: Request, token: str) -> HttpResponse:
        member = pictures.member_for_token(token)
        if member is None:
            raise Http404
        try:
            handle = member.picture_file.open("rb")
        except (FileNotFoundError, OSError):
            raise Http404 from None
        response = FileResponse(handle, content_type="image/jpeg")
        response["Cache-Control"] = "private, max-age=31536000, immutable"
        response["X-Content-Type-Options"] = "nosniff"
        response["Content-Disposition"] = "inline"
        return response
