"""
Series endpoints. Public (/api/blog/series/..., /api/blog/files/...): the series pages, episodes (full
text only with a member's reader key, `?k=`) and episode files. Staff (/api/admin/blog/series/...,
.../attachments/): managing series and an article's files.
"""
from __future__ import annotations

from typing import Any

from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from . import series as svc
from .models import Article, Attachment, NewsletterSubscriber, Series
from .serializers import (
    SeriesInputSerializer,
    StaffArticleSerializer,
    StaffSeriesSerializer,
    attachment_payload,
    image_payload,
)
from .services import BlogError


def _refused(exc: Exception) -> Response:
    return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


# --- Public payloads ----------------------------------------------------------------


def series_card(s: Series, released: int | None = None) -> dict[str, Any]:
    return {
        "slug": s.slug, "title": s.title, "tagline": s.tagline, "description": s.description,
        "cover": image_payload(s.cover_image), "cover_alt": s.cover_alt, "planned_episodes": s.planned_episodes,
        "cadence": s.cadence, "released": released,
    }


def episode_card(a: Article) -> dict[str, Any]:
    return {
        "slug": a.slug, "title": a.title, "season": a.season, "episode": a.episode, "summary": a.summary,
        "cover": image_payload(a.cover_image), "cover_alt": a.cover_alt, "reading_minutes": a.reading_minutes,
        "published_at": a.published_at, "free_preview": a.free_preview,
        "files": len(a.attachments.all()),
    }


def upcoming_card(a: Article) -> dict[str, Any]:
    """A scheduled episode: its number, title and date only (the rest stays a surprise)."""
    return {"slug": a.slug, "title": a.title, "season": a.season, "episode": a.episode, "published_at": a.published_at}


def reader_payload(sub: NewsletterSubscriber | None) -> dict[str, Any] | None:
    return {"email": svc.masked(sub.email)} if sub else None


# --- Public -------------------------------------------------------------------------


class PublicSeriesListView(APIView):
    """GET /api/blog/series/ -- every live series, with how many episodes are out."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request: Request) -> Response:
        rows = [series_card(s, len(svc.lineup(s).released)) for s in svc.live_series()]
        return Response({"results": rows})


class PublicSeriesView(APIView):
    """GET /api/blog/series/<slug>/?k= -- one series: seasons, released and upcoming episodes."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request: Request, slug: str) -> Response:
        s = svc.live_series().filter(slug=slug).first()
        if s is None:
            raise Http404("No such series.")
        line = svc.lineup(s)
        return Response({
            **series_card(s, len(line.released)),
            "seasons": svc.seasons(s, [*line.released, *line.upcoming]),
            "episodes": [episode_card(a) for a in line.released],
            "upcoming": [upcoming_card(a) for a in line.upcoming],
            "reader": reader_payload(svc.reader(request.query_params.get("k"))),
        })


class PublicEpisodeView(APIView):
    """GET /api/blog/series/<slug>/<episode>/?k= -- an episode. Members (a valid reader key) and free
    previews get the full text and files; everyone else the public opening and what's inside."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request: Request, slug: str, episode: str) -> Response:
        a = svc.released_episode(slug, episode)
        if a is None:
            raise Http404("No such episode.")
        sub = svc.reader(request.query_params.get("k"))
        unlocked = svc.can_read(a, sub)
        files = list(a.attachments.all())
        prev, nxt = svc.neighbours(a)
        upcoming = svc.lineup(a.series).upcoming
        return Response({
            **episode_card(a),
            "label": svc.label(a),
            "series": series_card(a.series),
            "planned_episodes": a.series.planned_episodes,
            "season_info": next((x for x in svc.seasons(a.series, [a]) if x["number"] == a.season), None),
            "seo_title": a.seo_title or a.title,
            "meta_description": a.meta_description or a.summary,
            "updated_at": a.updated_at,
            "teaser_html": a.teaser_html,
            "locked": not unlocked,
            "body_html": a.body_html if unlocked else "",
            # Locked: names and sizes only, so the reader sees what membership opens.
            "attachments": [
                attachment_payload(f) if unlocked else {"title": f.title, "content_type": f.content_type, "size": f.size}
                for f in files
            ],
            "previous": episode_card(prev) if prev else None,
            "next": episode_card(nxt) if nxt else None,
            "next_upcoming": upcoming_card(upcoming[0]) if (nxt is None and upcoming) else None,
            "reader": reader_payload(sub),
        })


class PublicFileView(APIView):
    """GET /api/blog/files/<key>/?k= -- an episode's file (members only for members-only episodes)."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request: Request, key: str) -> FileResponse | Response:
        att = Attachment.objects.select_related("article", "article__series").filter(key=key).first()
        if att is None:
            raise Http404("No such file.")
        access = svc.file_access(att, svc.reader(request.query_params.get("k")))
        if access == "missing":
            raise Http404("No such file.")
        if access == "members":
            return Response(
                {"detail": "This file is for subscribers. Open it from your episode email or subscribe on the series page."},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            handle = att.file.open("rb")
        except FileNotFoundError as exc:
            raise Http404("The file is missing.") from exc
        response = FileResponse(handle, content_type=att.content_type, as_attachment=True, filename=att.filename)
        response["Cache-Control"] = "private, no-store"
        response["X-Robots-Tag"] = "noindex"
        return response


# --- Staff --------------------------------------------------------------------------


class StaffSeriesListView(APIView):
    """GET /api/admin/blog/series/ -- every series. POST -- create one (a draft)."""

    permission_classes = [IsAdminUser]

    def get(self, request: Request) -> Response:
        qs = Series.objects.select_related("cover_image").all()
        return Response(StaffSeriesSerializer(qs, many=True).data)

    def post(self, request: Request) -> Response:
        data = SeriesInputSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            s = svc.save_series(None, data.validated_data)
        except BlogError as exc:
            return _refused(exc)
        return Response(StaffSeriesSerializer(s).data, status=status.HTTP_201_CREATED)


class StaffSeriesView(APIView):
    """GET / PATCH / DELETE /api/admin/blog/series/<id>/ (deleting keeps the episodes, as articles)."""

    permission_classes = [IsAdminUser]

    def get(self, request: Request, pk: int) -> Response:
        return Response(StaffSeriesSerializer(get_object_or_404(Series, pk=pk)).data)

    def patch(self, request: Request, pk: int) -> Response:
        s = get_object_or_404(Series, pk=pk)
        data = SeriesInputSerializer(data=request.data, partial=True)
        data.is_valid(raise_exception=True)
        try:
            s = svc.save_series(s, data.validated_data)
        except BlogError as exc:
            return _refused(exc)
        return Response(StaffSeriesSerializer(s).data)

    def delete(self, request: Request, pk: int) -> Response:
        s = get_object_or_404(Series, pk=pk)
        if s.episodes.filter(status=Article.Status.PUBLISHED).exists():
            return _refused(BlogError("Unpublish this series' episodes before deleting it."))
        s.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class StaffAttachmentsView(APIView):
    """POST /api/admin/blog/articles/<id>/attachments/ (multipart `file`, `title`) -> the article."""

    permission_classes = [IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request: Request, pk: int) -> Response:
        article = get_object_or_404(Article, pk=pk)
        upload = request.FILES.get("file")
        if upload is None:
            return Response({"detail": "Choose a file to upload."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            svc.save_attachment(article, upload, request.data.get("title", ""))
        except BlogError as exc:
            return _refused(exc)
        return Response(StaffArticleSerializer(article).data, status=status.HTTP_201_CREATED)


class StaffAttachmentView(APIView):
    """PATCH {title} / DELETE /api/admin/blog/attachments/<id>/ -> the article."""

    permission_classes = [IsAdminUser]

    def patch(self, request: Request, pk: int) -> Response:
        att = get_object_or_404(Attachment, pk=pk)
        title = str(request.data.get("title", "")).strip()[:120]
        if not title:
            return _refused(BlogError("Give the file a title."))
        att.title = title
        att.save(update_fields=["title"])
        return Response(StaffArticleSerializer(att.article).data)

    def delete(self, request: Request, pk: int) -> Response:
        att = get_object_or_404(Attachment, pk=pk)
        article = att.article
        att.file.delete(save=False)
        att.delete()
        return Response(StaffArticleSerializer(article).data)
