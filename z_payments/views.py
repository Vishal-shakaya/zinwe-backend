"""
Online payment endpoints (Razorpay Standard Checkout). See z_payments.services for the whole flow.

POST /api/payments/razorpay/order/    {upgrade_plan?, accept_terms, offer?}  -> the checkout options for a new order
GET  /api/payments/offer/<id>/?plan=  an admin offer this owner can use now, with its price (or 404)
POST /api/payments/razorpay/verify/   {razorpay_order_id, razorpay_payment_id, razorpay_signature}
POST /api/payments/razorpay/webhook/  Razorpay's server-to-server notice (signed with the webhook secret)
"""
from __future__ import annotations

import logging

from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from z_auth import billing
from z_auth.models import Plan
from z_auth.portal_serializers import PaymentSerializer

from . import razorpay_api as api
from . import services
from .models import RazorpayOrder

log = logging.getLogger(__name__)

TERMS = "Please accept the payment terms to continue."


class OrderRequest(serializers.Serializer):
    accept_terms = serializers.BooleanField(error_messages={"required": TERMS, "invalid": TERMS})
    upgrade_plan = serializers.SlugRelatedField(
        slug_field="code", queryset=Plan.objects.filter(is_active=True), required=False, allow_null=True,
        error_messages={"does_not_exist": "That plan isn't available.", "invalid": "That plan isn't available."},
    )
    # An admin offer (Announcement id) from a dashboard bar: checked again here, priced on the server.
    offer = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    # Monthly or yearly, as chosen at checkout (default: the subscription's own). Ignored with an offer.
    billing_cycle = serializers.ChoiceField(choices=("monthly", "yearly"), required=False, allow_null=True)

    def validate_accept_terms(self, value: bool) -> bool:
        if value is not True:
            raise serializers.ValidationError(TERMS)
        return value


class VerifyRequest(serializers.Serializer):
    razorpay_order_id = serializers.CharField(max_length=40)
    razorpay_payment_id = serializers.CharField(max_length=40)
    razorpay_signature = serializers.CharField(max_length=128)


def _unavailable(exc: Exception) -> Response:
    # `fallback`: the page switches to UPI / bank transfer + receipt, which never depend on Razorpay.
    return Response({"detail": str(exc), "fallback": True}, status=status.HTTP_503_SERVICE_UNAVAILABLE)


class RazorpayOrderView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "online_payment"

    def post(self, request: Request) -> Response:
        data = OrderRequest(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            order = services.start_order(
                request.user, data.validated_data.get("upgrade_plan"), accept_terms=True,
                offer_id=data.validated_data.get("offer"),
                cycle=data.validated_data.get("billing_cycle"),
            )
        except services.OnlinePaymentUnavailable as exc:
            return _unavailable(exc)
        except services.OfferNotAvailable as exc:
            return Response({"offer": [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)
        except (billing.NothingToPay, billing.PaymentUnderReview) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except billing.UpgradeNotAllowed as exc:
            return Response({"upgrade_plan": [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)
        return Response(services.checkout_options(order), status=status.HTTP_201_CREATED)


class OfferView(APIView):
    """The upgrade page asks this before showing an offer's price; the order checks it all again."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, pk: int) -> Response:
        plan = Plan.objects.filter(is_active=True, code=request.query_params.get("plan", "")).first()
        subscription = billing.current_subscription(request.user)
        try:
            offer = services.usable_offer(request.user, pk, plan)
        except services.OfferNotAvailable as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        try:
            if subscription is None:
                raise services.OfferNotAvailable()
            due = services.offer_quote(subscription, offer)
        except services.OfferNotAvailable as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        return Response({
            "id": offer.pk,
            "title": offer.title,
            "plan_code": offer.offer_plan.code,
            "plan_name": offer.offer_plan.name,
            "ends_at": offer.ends_at,
            "online_only": True,
            "online_enabled": services.online_enabled(),
            "due": due.as_dict(),
        })


class RazorpayVerifyView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "online_payment"

    def post(self, request: Request) -> Response:
        data = VerifyRequest(data=request.data)
        data.is_valid(raise_exception=True)
        v = data.validated_data
        try:
            result = services.confirm_from_checkout(
                request.user, v["razorpay_order_id"], v["razorpay_payment_id"], v["razorpay_signature"],
            )
        except RazorpayOrder.DoesNotExist:
            return Response({"detail": "That payment isn't for your account."}, status=status.HTTP_404_NOT_FOUND)
        except services.PaymentNotConfirmed as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except api.RazorpayError as exc:
            # The money may well be taken: never say it failed. The webhook / hourly check finish it.
            log.warning("Razorpay check failed for %s: %s", v["razorpay_order_id"], exc)
            return Response(
                {"status": "confirming", "detail": "Payment received. We're confirming it with the bank; your plan "
                 "updates within the hour and we'll email you."},
                status=status.HTTP_202_ACCEPTED,
            )
        payment = PaymentSerializer(result.payment).data if result.payment else None
        if result.order.status == RazorpayOrder.Status.NEEDS_REVIEW:
            return Response(
                {"status": "review", "payment": payment, "detail": "Payment received. Your plan changed meanwhile, "
                 "so we'll apply it by hand within one working day and email you."},
                status=status.HTTP_202_ACCEPTED,
            )
        return Response({"status": "paid", "payment": payment})


class RazorpayWebhookView(APIView):
    authentication_classes: list = []
    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        try:
            outcome = services.handle_webhook(request.body, request.headers.get("X-Razorpay-Signature", ""))
        except services.PaymentNotConfirmed as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except api.RazorpayError as exc:
            # Razorpay retries webhooks that don't get a 2xx.
            log.warning("Razorpay webhook: couldn't confirm: %s", exc)
            return Response({"detail": "retry"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({"status": outcome})
