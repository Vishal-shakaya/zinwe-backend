"""Thin HTTP layer for z_auth accounts. Validation in serializers, logic in services."""
from __future__ import annotations

from typing import Any

from rest_framework import status
from rest_framework.authentication import TokenAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle, SimpleRateThrottle
from rest_framework.views import APIView

from z_main.client import get_client_ip, get_user_agent

from . import entitlements, otp, services
from .announcements import announcements_for, website_announcements
from .billing import current_subscription
from .company import get_company
from .models import Plan, User
from .onboarding import onboarding_state
from .portal_serializers import CompanySerializer, SubscriptionSerializer
from .serializers import (
    EarlyAccessSerializer,
    EmailCodeSerializer,
    EmailSerializer,
    LoginSerializer,
    PasswordChangeSerializer,
    PasswordResetSerializer,
    PlanActivateSerializer,
    PlanActivationSerializer,
    PlanSerializer,
    RegisterSerializer,
    ResendCodeSerializer,
    SignupSerializer,
    TermsAcceptSerializer,
    UserSerializer,
    early_access_payload,
)

RESEND_DETAIL = "If the address can receive a code, a new one is on its way."
LOGIN_CODE_DETAIL = "If an account exists for this email, we've sent a sign-in code."
RESET_DETAIL = "If an account exists for this email, we've sent a reset code."


class PublicView(APIView):
    """Public endpoint: no session/CSRF coupling, per-IP scoped throttle."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes = [ScopedRateThrottle]


def auth_response(user: User) -> dict[str, Any]:
    """AuthResponse (portal-v1 §3.1): token + user + onboarding."""
    return {
        "token": services.get_token(user),
        "user": UserSerializer(user).data,
        "onboarding": onboarding_state(user),
    }


def throttled_response(exc: otp.OtpThrottled) -> Response:
    response = Response(
        {"detail": str(exc), "retry_after": exc.retry_after},
        status=status.HTTP_429_TOO_MANY_REQUESTS,
    )
    response["Retry-After"] = str(exc.retry_after)
    return response


def code_error() -> Response:
    return Response({"code": [services.InvalidCode.message]}, status=status.HTTP_400_BAD_REQUEST)


def code_sent(detail: str) -> Response:
    return Response({"detail": detail, "resend_in": otp.RESEND_COOLDOWN_SECONDS})


# --- Early access -------------------------------------------------------------


class EarlyAccessView(PublicView):
    """POST /api/auth/early-access/ -- public waitlist sign-up (throttled)."""

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


# --- Sign-up + codes ----------------------------------------------------------


class SignupView(PublicView):
    """POST /api/auth/signup/ -- create an unverified account and email a code."""

    throttle_scope = "auth_signup"

    def post(self, request: Request) -> Response:
        serializer = SignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        user, sent, resend_in = services.signup(
            data["full_name"], data["email"], data["password"], ip=get_client_ip(request), accepted_terms=True,
            source=data.get("source", ""),
        )
        return Response(
            {"email": user.email, "otp_sent": sent, "resend_in": resend_in},
            status=status.HTTP_201_CREATED,
        )


class SignupVerifyView(PublicView):
    """POST /api/auth/signup/verify/ -- activate the account with its code, sign in."""

    throttle_scope = "auth_otp"

    def post(self, request: Request) -> Response:
        serializer = EmailCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = services.verify_signup(**serializer.validated_data)
        except services.InvalidCode:
            return code_error()
        return Response(auth_response(user))


class ResendCodeView(PublicView):
    """POST /api/auth/otp/resend/ -- same generic answer whether or not the account exists."""

    throttle_scope = "auth_otp"

    def post(self, request: Request) -> Response:
        serializer = ResendCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.resend_code(
                serializer.validated_data["email"],
                serializer.validated_data["purpose"],
                ip=get_client_ip(request),
            )
        except otp.OtpThrottled as exc:
            return throttled_response(exc)
        return code_sent(RESEND_DETAIL)


# --- Sign-in ------------------------------------------------------------------


class RegisterView(PublicView):
    """
    POST /api/auth/register/ -- DEPRECATED: use `signup/` (email code) instead.

    Still creates an active (verified) account and returns `{token, user}` for
    older clients.
    """

    throttle_classes: list = []

    def post(self, request: Request) -> Response:
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = services.register_user(**serializer.validated_data)
        return Response(
            {"token": services.get_token(user), "user": UserSerializer(user).data},
            status=status.HTTP_201_CREATED,
        )


class FailedLoginThrottle(SimpleRateThrottle):
    """
    Counts only FAILED sign-ins, per email + IP (J5-22): many owners share one IP behind CGNAT, and a first
    correct attempt must never be locked out. The view calls `record_failure()` after a wrong password/code.
    """

    scope = "auth_login"

    def get_cache_key(self, request: Request, view: Any) -> str | None:
        email = ""
        try:
            email = str(request.data.get("email") or "").strip().lower()
        except Exception:  # noqa: BLE001 -- unparsable body: the serializer answers it
            email = ""
        return self.cache_format % {"scope": self.scope, "ident": f"{self.get_ident(request)}:{email}"}

    def throttle_success(self) -> bool:
        return True  # checking doesn't count; only failures do

    def record_failure(self, request: Request, view: Any) -> None:
        if self.rate is None:
            return
        key = self.get_cache_key(request, view)
        if key is None:
            return
        now = self.timer()
        history = [t for t in self.cache.get(key, []) if t > now - self.duration]
        history.insert(0, now)
        self.cache.set(key, history, self.duration)


class LoginIpThrottle(ScopedRateThrottle):
    """A high per-IP ceiling on every sign-in request (auth_login_ip), whatever the email."""

    scope_attr = "throttle_scope_ip"


class LoginView(PublicView):
    """POST /api/auth/login/ -- email + password -> AuthResponse (403 if unverified or paused)."""

    throttle_classes = [FailedLoginThrottle, LoginIpThrottle]
    throttle_scope_ip = "auth_login_ip"

    def post(self, request: Request) -> Response:
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = services.password_login(
                request,
                serializer.validated_data["email"],
                serializer.validated_data["password"],
            )
        except (services.EmailNotVerified, services.AccountPaused) as exc:
            return Response(
                {"code": exc.code, "detail": exc.message}, status=status.HTTP_403_FORBIDDEN
            )
        except services.InvalidCredentials as exc:
            FailedLoginThrottle().record_failure(request, self)
            return Response(
                {"non_field_errors": [exc.message]}, status=status.HTTP_400_BAD_REQUEST
            )
        return Response(auth_response(user))


class LoginCodeRequestView(PublicView):
    """POST /api/auth/login/otp/request/ -- email a sign-in code (generic answer)."""

    throttle_scope = "auth_otp"

    def post(self, request: Request) -> Response:
        serializer = EmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.request_login_code(
                serializer.validated_data["email"], ip=get_client_ip(request)
            )
        except otp.OtpThrottled as exc:
            return throttled_response(exc)
        return code_sent(LOGIN_CODE_DETAIL)


class LoginCodeVerifyView(PublicView):
    """POST /api/auth/login/otp/verify/ -- sign in with an emailed code (403 account_paused if blocked)."""

    throttle_classes = [FailedLoginThrottle, LoginIpThrottle]
    throttle_scope_ip = "auth_login_ip"

    def post(self, request: Request) -> Response:
        serializer = EmailCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = services.login_with_code(**serializer.validated_data)
        except services.AccountPaused as exc:
            return Response({"code": exc.code, "detail": exc.message}, status=status.HTTP_403_FORBIDDEN)
        except services.InvalidCode:
            FailedLoginThrottle().record_failure(request, self)
            return code_error()
        return Response(auth_response(user))


class LogoutView(APIView):
    """POST /api/auth/logout/ -- delete the API token."""

    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        services.logout(request.user, request.auth)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    """GET /api/auth/me/ -- the user, onboarding step, company, subscription and active plan."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        user = request.user
        company = get_company(user)
        subscription = current_subscription(user)
        activation = user.active_plan_activation
        ctx = {"user": user}
        return Response(
            {
                **UserSerializer(user).data,
                "onboarding": onboarding_state(user, company=company, subscription=subscription),
                "company": CompanySerializer(company).data if company else None,
                "subscription": (
                    SubscriptionSerializer(subscription, context=ctx).data if subscription else None
                ),
                "active_plan": PlanActivationSerializer(activation, context=ctx).data if activation else None,
                # What the plan switches on + the PC limit; the dashboard locks sections from it.
                "entitlements": entitlements.as_payload(user),
            }
        )


class TourDoneView(APIView):
    """POST /api/auth/tour/done/ -- the owner finished or skipped the dashboard tour: 204, never shown again."""

    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        services.mark_tour_done(request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class TermsAcceptView(APIView):
    """
    POST /api/auth/terms/accept/ -- signed in: {accept_terms: true}. Records the current Terms
    version for an owner who signed up before it (or before the Terms changed). Returns the user.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        TermsAcceptSerializer(data=request.data).is_valid(raise_exception=True)
        user = services.accept_terms(request.user)
        return Response(UserSerializer(user).data)


# --- Password reset -----------------------------------------------------------


class PasswordForgotView(PublicView):
    """POST /api/auth/password/forgot/ -- email a reset code (generic answer)."""

    throttle_scope = "auth_otp"

    def post(self, request: Request) -> Response:
        serializer = EmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.request_password_reset(
                serializer.validated_data["email"], ip=get_client_ip(request)
            )
        except otp.OtpThrottled as exc:
            return throttled_response(exc)
        return code_sent(RESET_DETAIL)


class PasswordResetView(PublicView):
    """POST /api/auth/password/reset/ -- new password with a code; old tokens revoked."""

    throttle_scope = "auth_otp"

    def post(self, request: Request) -> Response:
        serializer = PasswordResetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = services.reset_password(**serializer.validated_data)
        except services.PasswordRejected as exc:
            return Response({"new_password": exc.messages}, status=status.HTTP_400_BAD_REQUEST)
        except services.InvalidCode:
            return code_error()
        return Response(auth_response(user))


class PasswordChangeView(APIView):
    """
    POST /api/auth/password/change/ -- signed in: {current_password, new_password}.
    Rotates the token (every other device is signed out) and returns a fresh AuthResponse.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_login"

    def post(self, request: Request) -> Response:
        serializer = PasswordChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = services.change_password(request.user, **serializer.validated_data)
        except services.WrongPassword:
            return Response(
                {"current_password": ["That's not your current password."]}, status=status.HTTP_400_BAD_REQUEST
            )
        except services.PasswordRejected as exc:
            return Response({"new_password": exc.messages}, status=status.HTTP_400_BAD_REQUEST)
        return Response(auth_response(user))


# --- Plans ----------------------------------------------------------------------


class OptionalTokenAuthentication(TokenAuthentication):
    """Token auth for public pages: a missing, stale or revoked token just means anonymous."""

    def authenticate(self, request: Request) -> Any:
        try:
            return super().authenticate(request)
        except AuthenticationFailed:
            return None


class PlanListView(PublicView):
    """
    GET /api/auth/plans/ -- public list of active plans, by `sort_order`.

    The token is optional: with a valid one, `trial_available` reflects the
    caller's account (one free trial ever). Anonymous callers (or a stale
    token) see every trial as available.
    """

    authentication_classes = [OptionalTokenAuthentication]
    throttle_classes: list = []

    def get(self, request: Request) -> Response:
        plans = Plan.objects.filter(is_active=True).order_by("sort_order", "code")
        return Response(PlanSerializer(plans, many=True, context={"user": request.user}).data)


class WebsiteAnnouncementsView(APIView):
    """
    GET /api/auth/announcements/public/ -- the bars for the public website, sign-up and onboarding:
    the admin panel's live messages placed on the website (same for every visitor, so cacheable).
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes: list = []

    def get(self, request: Request) -> Response:
        response = Response({"announcements": website_announcements()})
        response["Cache-Control"] = "public, max-age=60"
        return response


class AnnouncementsView(APIView):
    """
    GET /api/auth/announcements/ -- the dashboard notification-bar items for the signed-in owner:
    the admin panel's messages for their audience, configured banners, then the upgrade bar.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes: list = []

    def get(self, request: Request) -> Response:
        return Response({"announcements": announcements_for(request.user)})


class PlanActivateView(APIView):
    """
    POST /api/auth/plans/activate/ -- switch the current user to a free/trial plan.

    Older endpoint; the portal uses `POST subscription/`. Paid plans are refused here.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        serializer = PlanActivateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        plan = serializer.validated_data["plan"]
        # Paid plans (even ones with a trial) are refused here; a free plan with
        # trial_days (legacy early access) is a trial.
        trial = bool(plan.trial_days) and not plan.requires_payment
        try:
            activation = services.activate_plan(request.user, plan, trial=trial)
        except services.PlanNotAllowed as exc:
            return Response({"plan": [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            PlanActivationSerializer(activation, context={"user": request.user}).data,
            status=status.HTTP_201_CREATED,
        )
