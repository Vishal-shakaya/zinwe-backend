"""
Owner side (/api/support/...) and staff side (/api/admin/support/...) of customer support.
"""
from __future__ import annotations

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import services
from .models import SupportTicket
from .serializers import (
    ContactSerializer,
    MessageSerializer,
    OpenTicketSerializer,
    ReplySerializer,
    StaffTicketDetailSerializer,
    StaffTicketSerializer,
    StatusSerializer,
    TicketDetailSerializer,
    TicketSerializer,
)

# --- Owner -------------------------------------------------------------------


class OwnerTicketsView(APIView):
    """GET /api/support/tickets/ -- your conversations (newest first, with unread replies).
    POST -- open one: {category, message, subject?, page?}. No subject = titled from the message.
    The team is emailed."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "support"

    def get(self, request: Request) -> Response:
        return Response(TicketSerializer(services.tickets_for_owner(request.user), many=True).data)

    def post(self, request: Request) -> Response:
        data = OpenTicketSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        v = data.validated_data
        ticket = services.open_ticket(request.user, v["subject"], v["category"], v["message"], v["page"])
        return Response(TicketDetailSerializer(ticket).data, status=status.HTTP_201_CREATED)

    def get_throttles(self):  # type: ignore[no-untyped-def]
        # Reading is free; only opening tickets is rate-limited.
        return super().get_throttles() if self.request.method == "POST" else []


class OwnerTicketView(APIView):
    """GET /api/support/tickets/<id>/ -- one conversation; marks our replies read."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, pk: int) -> Response:
        ticket = get_object_or_404(SupportTicket, pk=pk, owner=request.user)
        services.mark_read_by_owner(ticket)
        return Response(TicketDetailSerializer(ticket).data)


class OwnerReplyView(APIView):
    """POST /api/support/tickets/<id>/messages/ {body} -- add to a conversation (reopens it)."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "support"

    def post(self, request: Request, pk: int) -> Response:
        ticket = get_object_or_404(SupportTicket, pk=pk, owner=request.user)
        data = ReplySerializer(data=request.data)
        data.is_valid(raise_exception=True)
        message = services.owner_reply(request.user, ticket, data.validated_data["body"])
        return Response(MessageSerializer(message).data, status=status.HTTP_201_CREATED)


# --- Staff -------------------------------------------------------------------


class StaffTicketsView(APIView):
    """GET /api/admin/support/?status=open|answered|closed -- every conversation, plus counts."""

    permission_classes = [IsAdminUser]

    def get(self, request: Request) -> Response:
        wanted = request.query_params.get("status") or None
        if wanted and wanted not in SupportTicket.Status.values:
            wanted = None
        rows = StaffTicketSerializer(services.tickets_for_staff(wanted), many=True).data
        counts = {s: SupportTicket.objects.filter(status=s).count() for s in SupportTicket.Status.values}
        return Response({"counts": counts, "rows": rows})


class StaffTicketView(APIView):
    """GET /api/admin/support/<id>/ -- one conversation; marks the customer's messages read."""

    permission_classes = [IsAdminUser]

    def get(self, request: Request, pk: int) -> Response:
        ticket = get_object_or_404(services.tickets_for_staff(), pk=pk)
        services.mark_read_by_staff(ticket)
        return Response(StaffTicketDetailSerializer(ticket).data)


class StaffReplyView(APIView):
    """POST /api/admin/support/<id>/reply/ {body, close?} -- answer; the customer is emailed."""

    permission_classes = [IsAdminUser]

    def post(self, request: Request, pk: int) -> Response:
        ticket = get_object_or_404(SupportTicket, pk=pk)
        data = ReplySerializer(data=request.data)
        data.is_valid(raise_exception=True)
        services.staff_reply(request.user, ticket, data.validated_data["body"], data.validated_data["close"])
        fresh = get_object_or_404(services.tickets_for_staff(), pk=pk)
        return Response(StaffTicketDetailSerializer(fresh).data)


class StaffStatusView(APIView):
    """POST /api/admin/support/<id>/status/ {status} -- close or reopen without replying."""

    permission_classes = [IsAdminUser]

    def post(self, request: Request, pk: int) -> Response:
        ticket = get_object_or_404(SupportTicket, pk=pk)
        data = StatusSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        services.set_status(ticket, data.validated_data["status"])
        fresh = get_object_or_404(services.tickets_for_staff(), pk=pk)
        return Response(StaffTicketDetailSerializer(fresh).data)


# --- Public contact form -------------------------------------------------------


class ContactView(APIView):
    """
    POST /api/contact/ -- public website contact form: {name, email, company?, phone?, topic?, message}.
    Emails the team (reply-to = the visitor) and the visitor a "we got your message". 201 {"status": "sent"}.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "contact"

    def post(self, request: Request) -> Response:
        from z_main.client import get_client_ip, get_user_agent

        data = ContactSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        services.send_contact_message(
            data.validated_data, ip=get_client_ip(request), user_agent=get_user_agent(request)
        )
        return Response({"status": "sent"}, status=status.HTTP_201_CREATED)
