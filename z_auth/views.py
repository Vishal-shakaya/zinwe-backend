"""Thin HTTP layer for z_auth. Validation in serializers, logic in services."""
from __future__ import annotations

from django.contrib.auth import authenticate
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from z_main.client import get_client_ip, get_user_agent

from . import services
from .models import Plan
from .serializers import (
    EarlyAccessSerializer,
    LoginSerializer,
    PlanActivateSerializer,
    PlanActivationSerializer,
    PlanSerializer,
    RegisterSerializer,
    UserSerializer,
    early_access_payload,
)


class EarlyAccessView(APIView):
    """POST /api/auth/early-access/ -- public waitlist sign-up (throttled)."""

    permission_classes = [AllowAny]
    authentication_classes: list = []  # public form: no session/CSRF coupling
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "early_access"

    def post(self, request: Request) -> Response:
        serializer = EarlyAccessSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        registration, created = services.register_early_access(
            serializer.validated_data,
            ip=get_client_ip(request),
            user_agent=get_user_agent(request),
        )
        return Response(
            early_access_payload(registration, created),
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


def _auth_payload(user) -> dict:
    token, _ = Token.objects.get_or_create(user=user)
    return {"token": token.key, "user": UserSerializer(user).data}


class RegisterView(APIView):
    """POST /api/auth/register/ -- create an account, return a token."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def post(self, request: Request) -> Response:
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = services.register_user(**serializer.validated_data)
        return Response(_auth_payload(user), status=status.HTTP_201_CREATED)


class LoginView(APIView):
    """POST /api/auth/login/ -- exchange email + password for a token."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def post(self, request: Request) -> Response:
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = authenticate(
            request,
            email=serializer.validated_data["email"],
            password=serializer.validated_data["password"],
        )
        if user is None:
            return Response(
                {"non_field_errors": ["Email or password is incorrect."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(_auth_payload(user))


class MeView(APIView):
    """GET /api/auth/me/ -- the current user and their active plan."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        activation = request.user.active_plan_activation
        return Response(
            {
                **UserSerializer(request.user).data,
                "active_plan": PlanActivationSerializer(activation).data if activation else None,
            }
        )


class PlanListView(APIView):
    """GET /api/auth/plans/ -- public list of active plans."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request: Request) -> Response:
        plans = Plan.objects.filter(is_active=True)
        return Response(PlanSerializer(plans, many=True).data)


class PlanActivateView(APIView):
    """POST /api/auth/plans/activate/ -- switch the current user to a plan."""

    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        serializer = PlanActivateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            activation = services.activate_plan(request.user, serializer.validated_data["plan"])
        except services.PlanNotAllowed as exc:
            return Response({"plan": [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)
        return Response(PlanActivationSerializer(activation).data, status=status.HTTP_201_CREATED)
