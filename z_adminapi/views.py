"""Thin staff-only HTTP layer for the internal admin app. All views require IsAdminUser."""
from __future__ import annotations

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAdminUser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from z_auth.models import ManualPayment, Plan, User

from . import services
from .serializers import (
    ChangePlanSerializer,
    FeedbackRowSerializer,
    PaymentQueueSerializer,
    PlanSummarySerializer,
    RejectPaymentSerializer,
    RemarkSerializer,
    SendEmailSerializer,
    SentEmailSerializer,
    SetPasswordSerializer,
    SubscriberSerializer,
)


class StaffView(APIView):
    """Base: staff-only, no extra throttle (internal tool)."""

    permission_classes = [IsAdminUser]


def _refused(exc: Exception) -> Response:
    return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class MeView(StaffView):
    """GET /api/admin/me/ -- confirm the operator is staff; used by the app guard."""

    def get(self, request: Request) -> Response:
        user = request.user
        return Response(
            {"id": user.id, "email": user.email, "full_name": user.full_name, "is_staff": user.is_staff}
        )


class MetricsView(StaffView):
    """GET /api/admin/metrics/ -- dashboard totals + signup/revenue series."""

    def get(self, request: Request) -> Response:
        return Response(services.dashboard_metrics())


class PlanListView(StaffView):
    """GET /api/admin/plans/ -- active plans, for the plan picker."""

    def get(self, request: Request) -> Response:
        plans = Plan.objects.filter(is_active=True).order_by("sort_order", "code")
        return Response(PlanSummarySerializer(plans, many=True).data)


# --- Subscribers ---------------------------------------------------------------


class SubscriberListView(StaffView):
    """GET /api/admin/subscribers/ -- every account owner with plan, payments and cycles."""

    def get(self, request: Request) -> Response:
        return Response(SubscriberSerializer(services.subscribers(), many=True).data)


def _owner_or_404(pk: int) -> User:
    return get_object_or_404(User, pk=pk, is_staff=False)


class SubscriberDetailView(StaffView):
    """GET /api/admin/subscribers/<id>/ -- one owner.  DELETE -- delete the account for good."""

    def get(self, request: Request, pk: int) -> Response:
        try:
            view = services.subscriber(pk)
        except User.DoesNotExist:
            return Response({"detail": "No such customer."}, status=status.HTTP_404_NOT_FOUND)
        return Response(SubscriberSerializer(view).data)

    def delete(self, request: Request, pk: int) -> Response:
        user = _owner_or_404(pk)
        try:
            services.delete_customer(user)
        except services.AdminActionError as exc:
            return _refused(exc)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ApprovePaymentView(StaffView):
    """POST /api/admin/subscribers/<id>/approve-payment/ -- verify the latest payment."""

    def post(self, request: Request, pk: int) -> Response:
        user = _owner_or_404(pk)
        try:
            view = services.approve_payment(user, request.user)
        except services.AdminActionError as exc:
            return _refused(exc)
        return Response(SubscriberSerializer(view).data)


class RejectPaymentView(StaffView):
    """POST /api/admin/subscribers/<id>/reject-payment/ -- reject the latest payment."""

    def post(self, request: Request, pk: int) -> Response:
        user = _owner_or_404(pk)
        serializer = RejectPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            view = services.reject_payment(user, request.user, serializer.validated_data["reason"])
        except services.AdminActionError as exc:
            return _refused(exc)
        return Response(SubscriberSerializer(view).data)


class BlockView(StaffView):
    """POST /api/admin/subscribers/<id>/block/ (or unblock/) -- toggle account access."""

    blocked = True

    def post(self, request: Request, pk: int) -> Response:
        user = _owner_or_404(pk)
        try:
            view = services.set_blocked(user, self.blocked)
        except services.AdminActionError as exc:
            return _refused(exc)
        return Response(SubscriberSerializer(view).data)


class UnblockView(BlockView):
    blocked = False


class ChangePlanView(StaffView):
    """POST /api/admin/subscribers/<id>/change-plan/ -- grant a plan immediately."""

    def post(self, request: Request, pk: int) -> Response:
        user = _owner_or_404(pk)
        serializer = ChangePlanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            view = services.change_plan(user, serializer.validated_data["plan"])
        except services.AdminActionError as exc:
            return _refused(exc)
        return Response(SubscriberSerializer(view).data)


class SetPasswordView(StaffView):
    """POST /api/admin/subscribers/<id>/set-password/ -- {password}; signs the customer out everywhere."""

    def post(self, request: Request, pk: int) -> Response:
        user = _owner_or_404(pk)
        serializer = SetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            view = services.set_password(user, serializer.validated_data["password"])
        except services.AdminActionError as exc:
            return _refused(exc)
        return Response(SubscriberSerializer(view).data)


class RemarkView(StaffView):
    """POST /api/admin/subscribers/<id>/remark/ -- {text}; the team's internal note (empty clears it)."""

    def post(self, request: Request, pk: int) -> Response:
        user = _owner_or_404(pk)
        serializer = RemarkSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        view = services.set_remark(user, serializer.validated_data["text"], request.user)
        return Response(SubscriberSerializer(view).data)


class CustomerEmailView(StaffView):
    """
    GET  /api/admin/subscribers/<id>/emails/ -- custom emails the team sent them, newest first.
    POST /api/admin/subscribers/<id>/emails/ -- {subject, html}; html is cleaned to a safe allow-list.
    """

    def get(self, request: Request, pk: int) -> Response:
        user = _owner_or_404(pk)
        return Response(SentEmailSerializer(services.customer_emails(user), many=True).data)

    def post(self, request: Request, pk: int) -> Response:
        user = _owner_or_404(pk)
        serializer = SendEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            sent = services.send_customer_email(
                user, serializer.validated_data["subject"], serializer.validated_data["html"], request.user
            )
        except services.AdminActionError as exc:
            return _refused(exc)
        return Response(SentEmailSerializer(sent).data, status=status.HTTP_201_CREATED)


# --- Plan activation (payment review queue) -------------------------------------


class PaymentListView(StaffView):
    """GET /api/admin/payments/?status=submitted|verified|rejected -- reported payments, newest first."""

    def get(self, request: Request) -> Response:
        wanted = request.query_params.get("status") or None
        if wanted and wanted not in ManualPayment.Status.values:
            return Response({"detail": f"Unknown status '{wanted}'."}, status=status.HTTP_400_BAD_REQUEST)
        return Response(PaymentQueueSerializer(services.payments(wanted), many=True).data)


class PaymentApproveView(StaffView):
    """POST /api/admin/payments/<id>/approve/ -- the plan activates only now."""

    def post(self, request: Request, pk: int) -> Response:
        get_object_or_404(ManualPayment, pk=pk)
        try:
            view = services.review_payment(pk, request.user, approve=True)
        except services.AdminActionError as exc:
            return _refused(exc)
        return Response(PaymentQueueSerializer(view).data)


class PaymentRejectView(StaffView):
    """POST /api/admin/payments/<id>/reject/ -- {reason}; the customer sees it and can resubmit."""

    def post(self, request: Request, pk: int) -> Response:
        get_object_or_404(ManualPayment, pk=pk)
        serializer = RejectPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            view = services.review_payment(
                pk, request.user, approve=False, reason=serializer.validated_data["reason"]
            )
        except services.AdminActionError as exc:
            return _refused(exc)
        return Response(PaymentQueueSerializer(view).data)


# --- Feedback ---------------------------------------------------------------------


class FeedbackListView(StaffView):
    """GET /api/admin/feedback/ -- {summary: {count, average, distribution}, rows: [...]}."""

    def get(self, request: Request) -> Response:
        return Response(
            {
                "summary": services.feedback_summary(),
                "rows": FeedbackRowSerializer(services.feedback_rows(), many=True).data,
            }
        )
