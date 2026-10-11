"""Owner portal endpoints: company, subscription, manual payment. Thin; logic in company.py / billing.py."""
from __future__ import annotations

import mimetypes
import os

from django.http import FileResponse, Http404
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import billing
from .company import CompanyExists, get_company, register_company, update_company
from .models import Feedback, ManualPayment
from .portal_serializers import (
    CompanySerializer,
    FeedbackSerializer,
    PaymentSerializer,
    PaymentSubmitSerializer,
    SubscribeSerializer,
    SubscriptionSerializer,
)
from .services import PlanNotAllowed

NO_COMPANY = {"detail": "No company yet."}


class CompanyView(APIView):
    """GET / POST / PATCH /api/auth/company/ -- the owner's single company."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        company = get_company(request.user)
        if company is None:
            return Response(NO_COMPANY, status=status.HTTP_404_NOT_FOUND)
        return Response(CompanySerializer(company).data)

    def post(self, request: Request) -> Response:
        if get_company(request.user) is not None:
            return Response({"detail": CompanyExists.message}, status=status.HTTP_409_CONFLICT)
        serializer = CompanySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            company = register_company(request.user, serializer.validated_data)
        except CompanyExists as exc:
            return Response({"detail": exc.message}, status=status.HTTP_409_CONFLICT)
        return Response(CompanySerializer(company).data, status=status.HTTP_201_CREATED)

    def patch(self, request: Request) -> Response:
        company = get_company(request.user)
        if company is None:
            return Response(NO_COMPANY, status=status.HTTP_404_NOT_FOUND)
        serializer = CompanySerializer(company, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        company = update_company(company, serializer.validated_data)
        return Response(CompanySerializer(company).data)


class SubscriptionView(APIView):
    """GET / POST /api/auth/subscription/ -- current subscription + payment instructions; subscribe."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        subscription = billing.current_subscription(request.user)
        return Response(
            {
                "subscription": (
                    SubscriptionSerializer(subscription, context={"user": request.user}).data
                    if subscription else None
                ),
                "instructions": billing.payment_instructions(),
            }
        )

    def post(self, request: Request) -> Response:
        serializer = SubscribeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            subscription = billing.subscribe(request.user, serializer.validated_data["plan"])
        except billing.CompanyRequired as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except PlanNotAllowed as exc:
            return Response({"plan": [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)
        except (billing.AlreadyActive, billing.PaymentUnderReview) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(
            SubscriptionSerializer(subscription, context={"user": request.user}).data,
            status=status.HTTP_201_CREATED,
        )


class PaymentSubmitView(APIView):
    """POST /api/auth/subscription/payment/ (multipart) -- report a manual payment for review."""

    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "payment_submit"

    def post(self, request: Request) -> Response:
        serializer = PaymentSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        proof = data.pop("proof", None)
        try:
            payment = billing.submit_payment(request.user, data, proof)
        except (billing.NothingToPay, billing.PaymentUnderReview) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except billing.UpgradeNotAllowed as exc:
            return Response({"upgrade_plan": [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)
        return Response(PaymentSerializer(payment).data, status=status.HTTP_201_CREATED)


def proof_file_response(payment: ManualPayment) -> FileResponse:
    """Stream a payment's proof as an attachment. Raises Http404 if there is none."""
    if not payment.proof:
        raise Http404("No proof file.")
    try:
        handle = payment.proof.open("rb")
    except FileNotFoundError as exc:
        raise Http404("The proof file is missing.") from exc
    filename = f"payment-{payment.pk}-proof{os.path.splitext(payment.proof.name)[1]}"
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(handle, as_attachment=True, filename=filename, content_type=content_type)


class PaymentHistoryView(APIView):
    """GET /api/auth/subscription/payments/ -- every payment the owner reported, newest first."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        payments = (
            ManualPayment.objects.filter(subscription__owner=request.user)
            .select_related("subscription__plan")
            .order_by("-submitted_at", "-id")[:50]
        )
        return Response(PaymentSerializer(payments, many=True).data)


class PaymentProofView(APIView):
    """GET /api/auth/subscription/payment/<id>/proof/ -- the owner's own proof file (staff: any)."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, payment_id: int) -> FileResponse:
        payments = ManualPayment.objects.all()
        if not request.user.is_staff:
            payments = payments.filter(subscription__owner=request.user)
        payment = payments.filter(pk=payment_id).first()
        if payment is None:
            raise Http404("No such payment.")
        return proof_file_response(payment)


class FeedbackView(APIView):
    """
    GET  /api/auth/feedback/ -- the owner's own feedback, newest first (last 10).
    POST /api/auth/feedback/ -- {rating 1-10, message?, page?}; the team reads it in the admin app.
    """

    permission_classes = [IsAuthenticated]
    throttle_scope = "feedback"

    def get_throttles(self):  # type: ignore[no-untyped-def]
        # Only sending is rate-limited; reading your own history is not.
        return [ScopedRateThrottle()] if self.request.method == "POST" else []

    def get(self, request: Request) -> Response:
        rows = Feedback.objects.filter(owner=request.user)[:10]
        return Response(FeedbackSerializer(rows, many=True).data)

    def post(self, request: Request) -> Response:
        serializer = FeedbackSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        feedback = serializer.save(owner=request.user)
        return Response(FeedbackSerializer(feedback).data, status=status.HTTP_201_CREATED)
