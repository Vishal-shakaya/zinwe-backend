"""
Guide endpoints. Public (/api/blog/guides/...): the library, a guide's page (with a download link for members,
`?k=`) and the members' download. Staff (/api/admin/blog/guides/...): managing guides, their PDF and numbers.
"""
from __future__ import annotations

from typing import Any

from django.db.models import Count, Sum
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from rest_framework import serializers, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from . import guides as svc
from . import series
from .models import BlogImage, Guide, NewsletterSubscriber
from .serializers import image_payload
from .services import BlogError


def _refused(exc: Exception) -> Response:
    return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


def detail(g: Guide, sub: NewsletterSubscriber | None) -> dict[str, Any]:
    return {
        **svc.card(g),
        "description": g.description,
        "contents": g.contents or [],
        "feature_line": g.feature_line,
        "samples": [image_payload(img) for img in svc.samples(g)],
        "size": g.size,
        "filename": g.filename,
        "updated_at": g.updated_at,
        "member": sub is not None,
        "download_url": f"/api/blog/guides/{g.slug}/download/" if sub is not None else None,
        "reader": {"email": series.masked(sub.email), "ref_code": sub.ref_code} if sub else None,
        "others": [svc.card(o) for o in svc.live().exclude(pk=g.pk)[:3]],
    }


class PublicGuidesView(APIView):
    """GET /api/blog/guides/ -- every published guide."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request: Request) -> Response:
        return Response({"results": [svc.card(g) for g in svc.live()]})


class PublicGuideView(APIView):
    """GET /api/blog/guides/<slug>/?k= -- a guide's page; members also get the download link."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request: Request, slug: str) -> Response:
        g = svc.live().filter(slug=slug).first()
        if g is None:
            raise Http404("No such guide.")
        return Response(detail(g, series.reader(request.query_params.get("k"))))


class PublicGuideDownloadView(APIView):
    """GET /api/blog/guides/<slug>/download/?k= -- the PDF, for members (403 otherwise)."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request: Request, slug: str) -> FileResponse | Response:
        g = svc.live().filter(slug=slug).first()
        if g is None or not g.file:
            raise Http404("No such guide.")
        sub = series.reader(request.query_params.get("k"))
        if sub is None:
            return Response(
                {"detail": "This guide is free for members. Enter your email on the guide's page to get it."},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            handle = g.file.open("rb")
        except FileNotFoundError as exc:
            raise Http404("The file is missing.") from exc
        svc.record_download(g, sub)
        response = FileResponse(handle, content_type="application/pdf", as_attachment=True, filename=g.filename or "zinwe-guide.pdf")
        response["Cache-Control"] = "private, no-store"
        response["X-Robots-Tag"] = "noindex"
        return response


# --- Staff --------------------------------------------------------------------------


class GuideInput(serializers.Serializer):
    title = serializers.CharField(max_length=160)
    slug = serializers.CharField(max_length=160, required=False, allow_blank=True)
    subtitle = serializers.CharField(max_length=300, required=False, allow_blank=True)
    summary = serializers.CharField(max_length=300, required=False, allow_blank=True)
    description = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    audience = serializers.CharField(max_length=200, required=False, allow_blank=True)
    cover_image = serializers.PrimaryKeyRelatedField(queryset=BlogImage.objects.all(), required=False, allow_null=True)
    cover_alt = serializers.CharField(max_length=200, required=False, allow_blank=True)
    sample_pages = serializers.ListField(child=serializers.IntegerField(), required=False)
    contents = serializers.JSONField(required=False)
    highlights = serializers.JSONField(required=False)
    feature_line = serializers.CharField(max_length=300, required=False, allow_blank=True)
    tags = serializers.CharField(max_length=200, required=False, allow_blank=True)
    campaign = serializers.CharField(max_length=24, required=False, allow_blank=True)
    filename = serializers.CharField(max_length=120, required=False, allow_blank=True)
    status = serializers.ChoiceField(choices=Guide.Status.choices, required=False)
    position = serializers.IntegerField(min_value=0, max_value=999, required=False)


def staff_payload(g: Guide) -> dict[str, Any]:
    from z_auth.models import User

    stats = g.downloads.aggregate(members=Count("id"), downloads=Sum("downloads"))
    return {
        "id": g.id, **svc.card(g), "description": g.description, "contents": g.contents or [],
        "feature_line": g.feature_line, "tags": g.tags, "status": g.status, "position": g.position,
        "sample_pages": [image_payload(img) for img in svc.samples(g)],
        "has_file": bool(g.file), "filename": g.filename, "size": g.size,
        "stats": {
            "members_downloaded": stats["members"] or 0,
            "downloads": stats["downloads"] or 0,
            "joined_on_page": NewsletterSubscriber.objects.filter(source__startswith=svc.path(g)).count(),
            "signups": User.objects.filter(signup_source__startswith=f"{svc.campaign(g)}:").count(),
        },
        "updated_at": g.updated_at,
    }


class StaffGuidesView(APIView):
    """GET /api/admin/blog/guides/ -- every guide with its numbers. POST -- create one (a draft)."""

    permission_classes = [IsAdminUser]

    def get(self, request: Request) -> Response:
        return Response([staff_payload(g) for g in Guide.objects.select_related("cover_image")])

    def post(self, request: Request) -> Response:
        data = GuideInput(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            g = svc.save(None, data.validated_data)
        except BlogError as exc:
            return _refused(exc)
        return Response(staff_payload(g), status=status.HTTP_201_CREATED)


class StaffGuideView(APIView):
    """GET / PATCH / DELETE /api/admin/blog/guides/<id>/."""

    permission_classes = [IsAdminUser]

    def get(self, request: Request, pk: int) -> Response:
        return Response(staff_payload(get_object_or_404(Guide, pk=pk)))

    def patch(self, request: Request, pk: int) -> Response:
        g = get_object_or_404(Guide, pk=pk)
        data = GuideInput(data=request.data, partial=True)
        data.is_valid(raise_exception=True)
        try:
            g = svc.save(g, data.validated_data)
        except BlogError as exc:
            return _refused(exc)
        return Response(staff_payload(g))

    def delete(self, request: Request, pk: int) -> Response:
        g = get_object_or_404(Guide, pk=pk)
        if g.file:
            g.file.delete(save=False)
        g.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class StaffGuideFileView(APIView):
    """POST /api/admin/blog/guides/<id>/file/ (multipart `file`, a PDF up to 9 MB) -> the guide."""

    permission_classes = [IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request: Request, pk: int) -> Response:
        g = get_object_or_404(Guide, pk=pk)
        upload = request.FILES.get("file")
        if upload is None:
            return _refused(BlogError("Choose the PDF to upload."))
        try:
            g = svc.attach_pdf(g, upload)
        except BlogError as exc:
            return _refused(exc)
        return Response(staff_payload(g))
