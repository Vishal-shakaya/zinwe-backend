"""
Newsletter endpoints. Public (/api/blog/newsletter/...): sign up, confirm, unsubscribe (also the
one-click List-Unsubscribe URL in every email). Staff (/api/admin/blog/...): subscribers, and
emailing an article.
"""
from __future__ import annotations

from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from rest_framework import serializers, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from . import newsletter
from .models import Article, NewsletterIssue, NewsletterSubscriber
from .services import BlogError


def _refused(exc: Exception) -> Response:
    return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class SignupThrottle(AnonRateThrottle):
    scope = "newsletter"
    rate = "10/hour"


class SubscribeInput(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    source = serializers.CharField(max_length=240, required=False, allow_blank=True)


class TokenInput(serializers.Serializer):
    token = serializers.CharField(max_length=64)


class SubscribeView(APIView):
    """POST /api/blog/newsletter/subscribe/ {email, source?} -> always "check your inbox"."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes = [SignupThrottle]

    def post(self, request: Request) -> Response:
        data = SubscribeInput(data=request.data)
        data.is_valid(raise_exception=True)
        newsletter.subscribe(data.validated_data["email"], data.validated_data.get("source", ""))
        return Response({"status": "check_inbox"}, status=status.HTTP_202_ACCEPTED)


class ConfirmView(APIView):
    """POST /api/blog/newsletter/confirm/ {token}."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def post(self, request: Request) -> Response:
        data = TokenInput(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            sub = newsletter.confirm(data.validated_data["token"])
        except BlogError as exc:
            return _refused(exc)
        return Response({"status": sub.status, "email": sub.email})


class UnsubscribeView(APIView):
    """POST /api/blog/newsletter/unsubscribe/ {token} (the website's page), or
    POST /api/blog/newsletter/unsubscribe/<token>/ (RFC 8058 one-click, sent by the mail client)."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def post(self, request: Request, token: str | None = None) -> Response:
        if token is None:
            data = TokenInput(data=request.data)
            data.is_valid(raise_exception=True)
            token = data.validated_data["token"]
        try:
            sub = newsletter.unsubscribe(token)
        except BlogError as exc:
            return _refused(exc)
        return Response({"status": sub.status, "email": sub.email})


one_click_unsubscribe = csrf_exempt(UnsubscribeView.as_view())


# --- Staff ------------------------------------------------------------------------


class SubscriberSerializer(serializers.ModelSerializer):
    class Meta:
        model = NewsletterSubscriber
        fields = ["id", "email", "status", "source", "created_at", "confirmed_at", "unsubscribed_at"]


class StaffSubscribersView(APIView):
    """GET /api/admin/blog/subscribers/?status=&q= -> {counts, rows}."""

    permission_classes = [IsAdminUser]

    def get(self, request: Request) -> Response:
        rows = newsletter.subscriber_rows(request.query_params.get("status", ""), request.query_params.get("q", ""))
        return Response({"counts": newsletter.counts(), "rows": SubscriberSerializer(rows, many=True).data})


class StaffSubscriberView(APIView):
    """DELETE /api/admin/blog/subscribers/<id>/ -- erase the address (e.g. on request)."""

    permission_classes = [IsAdminUser]

    def delete(self, request: Request, pk: int) -> Response:
        get_object_or_404(NewsletterSubscriber, pk=pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class QueueInput(serializers.Serializer):
    subject = serializers.CharField(max_length=200, required=False, allow_blank=True)


def _state(article: Article) -> dict:
    issue = NewsletterIssue.objects.filter(article=article).select_related("queued_by", "article").first()
    return {
        "audience": newsletter.audience(),
        "default_subject": newsletter.default_subject(article),
        "full_article": article.type == Article.Type.NEWSLETTER,
        "issue": newsletter.progress(issue),
    }


class StaffArticleNewsletterView(APIView):
    """GET /api/admin/blog/articles/<id>/newsletter/ -> audience + this article's send (if any).
    POST {subject?} -> email it to every confirmed subscriber (once per article)."""

    permission_classes = [IsAdminUser]

    def get(self, request: Request, pk: int) -> Response:
        return Response(_state(get_object_or_404(Article, pk=pk)))

    def post(self, request: Request, pk: int) -> Response:
        article = get_object_or_404(Article, pk=pk)
        data = QueueInput(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            newsletter.queue(article, data.validated_data.get("subject", ""), request.user)
        except BlogError as exc:
            return _refused(exc)
        return Response(_state(article), status=status.HTTP_201_CREATED)


class StaffNewsletterTestView(APIView):
    """POST /api/admin/blog/articles/<id>/newsletter/test/ -> one copy to the signed-in staff member."""

    permission_classes = [IsAdminUser]

    def post(self, request: Request, pk: int) -> Response:
        article = get_object_or_404(Article, pk=pk)
        try:
            note = newsletter.send_test(article, request.user.email)
        except BlogError as exc:
            return _refused(exc)
        if note is None or note.status != "sent":
            return Response(
                {"detail": "The test email couldn't be sent. Check the email settings (Notifications in Django admin)."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response({"sent_to": request.user.email})


class StaffNewsletterRetryView(APIView):
    """POST /api/admin/blog/articles/<id>/newsletter/retry/ -> send the failed ones again."""

    permission_classes = [IsAdminUser]

    def post(self, request: Request, pk: int) -> Response:
        article = get_object_or_404(Article, pk=pk)
        issue = get_object_or_404(NewsletterIssue, article=article)
        newsletter.retry_failed(issue)
        return Response(_state(article))
