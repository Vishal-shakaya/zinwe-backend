"""
Public side (/api/blog/...: the website's /blog) and staff side (/api/admin/blog/...: the admin
app's Blog tab) of the blog.
"""
from __future__ import annotations

from xml.sax.saxutils import escape

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from z_analytics.services import track

from . import newsletter, series, services
from .models import Article, BlogImage
from .serializers import (
    ArticleCardSerializer,
    ArticleDetailSerializer,
    ArticleInputSerializer,
    PublishInputSerializer,
    StaffArticleSerializer,
    image_payload,
)

PAGE_SIZE = 9


def _refused(exc: Exception) -> Response:
    return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


def _audit(request: Request, action: str, article: Article, **extra: object) -> None:
    """Staff audit trail (FIX-007): "admin.blog_article_<action>", like z_adminapi's. Never raises."""
    track(f"admin.blog_article_{action}", {"id": article.pk, "title": article.title, **extra},
          actor_email=request.user.email, ref=article if article.pk else None)


# --- Public ---------------------------------------------------------------------


class PublicArticlesView(APIView):
    """GET /api/blog/articles/?type=&tag=&page= -- published articles, newest first, 9 a page."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request: Request) -> Response:
        qs = services.published()
        wanted = request.query_params.get("type") or ""
        if wanted:
            if wanted not in Article.Type.values:
                return Response({"detail": f"Unknown type '{wanted}'."}, status=status.HTTP_400_BAD_REQUEST)
            qs = qs.filter(type=wanted)
        tag = (request.query_params.get("tag") or "").strip().lower()
        if tag:
            qs = qs.filter(tags__icontains=tag)
        try:
            page = max(1, int(request.query_params.get("page") or 1))
        except ValueError:
            page = 1
        count = qs.count()
        pages = max(1, -(-count // PAGE_SIZE))
        rows = qs[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
        return Response({
            "count": count, "page": page, "pages": pages,
            "types": services.type_counts(),
            "results": ArticleCardSerializer(rows, many=True).data,
        })


class PublicArticleView(APIView):
    """GET /api/blog/articles/<slug>/ -- one published article + up to 3 related ones."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request: Request, slug: str) -> Response:
        article = services.published().filter(slug=slug).first()
        if article is None:
            raise Http404("No such article.")
        pick = series.related_episode(article)
        from . import guides

        gpick = guides.for_article(article)
        return Response({
            **ArticleDetailSerializer(article).data,
            "related": ArticleCardSerializer(services.related(article), many=True).data,
            # "From the members' series": the episode closest to this article's topic.
            "series_pick": None if pick is None else {
                "series_slug": pick.series.slug, "series_title": pick.series.title, "slug": pick.slug,
                "title": pick.title, "label": series.label(pick), "summary": pick.summary, "free_preview": pick.free_preview,
                "cover": image_payload(pick.cover_image or pick.series.cover_image),
            },
            # "Free guide": the e-guide closest to this article's topic.
            "guide_pick": None if gpick is None else guides.card(gpick),
        })


class PublicImageView(APIView):
    """GET /api/blog/images/<key>/ -- an uploaded blog image (public, long-cached)."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request: Request, key: str) -> FileResponse:
        img = get_object_or_404(BlogImage, key=key)
        try:
            handle = img.file.open("rb")
        except FileNotFoundError as exc:
            raise Http404("The image file is missing.") from exc
        response = FileResponse(handle, content_type=img.content_type)
        response["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def sitemap(request: Request) -> HttpResponse:
    """GET /api/blog/sitemap.xml -- every public blog page (z_blog/sitemap.py). Kept for anything that already points
    here; search engines are given /sitemap.xml (z_main/seo.py), which includes all of these."""
    from .sitemap import entries

    base = settings.ZINWE_APP_URL
    body = "".join(
        f"<url><loc>{escape(base + path)}</loc><lastmod>{when:%Y-%m-%d}</lastmod></url>" for path, when in entries()
    )
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>'
    return HttpResponse(xml, content_type="application/xml")


def rss(request: Request) -> HttpResponse:
    """GET /api/blog/rss.xml -- the latest 20 articles as an RSS feed."""
    base = settings.ZINWE_APP_URL
    items = "".join(
        "<item>"
        f"<title>{escape(a.title)}</title><link>{escape(f'{base}/blog/{a.slug}')}</link>"
        f"<guid>{escape(f'{base}/blog/{a.slug}')}</guid><category>{escape(a.get_type_display())}</category>"
        f"<description>{escape(a.summary)}</description>"
        f"<pubDate>{a.published_at:%a, %d %b %Y %H:%M:%S %z}</pubDate>"
        "</item>"
        for a in services.published()[:20]
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<title>Zinwe blog</title><link>{escape(base)}/blog</link>"
        "<description>News, guides and releases from Zinwe.</description>"
        f"{items}</channel></rss>"
    )
    return HttpResponse(xml, content_type="application/rss+xml")


# --- Staff ------------------------------------------------------------------------


class StaffArticlesView(APIView):
    """GET /api/admin/blog/articles/?status=&type=&q= -- every article (drafts too). POST -- create a draft."""

    permission_classes = [IsAdminUser]

    def get(self, request: Request) -> Response:
        qs = (
            Article.objects.select_related("cover_image", "author", "series")
            .prefetch_related("attachments")
            .order_by("-updated_at", "-id")
        )
        wanted = request.query_params.get("status")
        now = timezone.now()
        if wanted == "draft":
            qs = qs.filter(status=Article.Status.DRAFT)
        elif wanted == "scheduled":
            qs = qs.filter(status=Article.Status.PUBLISHED, published_at__gt=now)
        elif wanted == "published":
            qs = qs.filter(status=Article.Status.PUBLISHED, published_at__lte=now)
        if request.query_params.get("type") in Article.Type.values:
            qs = qs.filter(type=request.query_params["type"])
        q = (request.query_params.get("q") or "").strip()
        if q:
            qs = qs.filter(title__icontains=q)
        series = request.query_params.get("series") or ""
        if series == "none":
            qs = qs.filter(series__isnull=True)
        elif series.isdigit():
            qs = qs.filter(series_id=int(series))
        return Response(StaffArticleSerializer(qs, many=True).data)

    def post(self, request: Request) -> Response:
        data = ArticleInputSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            article = services.save_article(None, data.validated_data, request.user)
        except services.BlogError as exc:
            return _refused(exc)
        _audit(request, "created", article)
        return Response(StaffArticleSerializer(article).data, status=status.HTTP_201_CREATED)


class StaffArticleView(APIView):
    """GET / PATCH / DELETE /api/admin/blog/articles/<id>/."""

    permission_classes = [IsAdminUser]

    def get(self, request: Request, pk: int) -> Response:
        return Response(StaffArticleSerializer(get_object_or_404(Article, pk=pk)).data)

    def patch(self, request: Request, pk: int) -> Response:
        article = get_object_or_404(Article, pk=pk)
        data = ArticleInputSerializer(data=request.data, partial=True)
        data.is_valid(raise_exception=True)
        try:
            article = services.save_article(article, data.validated_data, request.user)
        except services.BlogError as exc:
            return _refused(exc)
        _audit(request, "updated", article, fields=sorted(data.validated_data))
        return Response(StaffArticleSerializer(article).data)

    def delete(self, request: Request, pk: int) -> Response:
        article = get_object_or_404(Article, pk=pk)
        _audit(request, "deleted", article)
        article.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class StaffPublishView(APIView):
    """POST /api/admin/blog/articles/<id>/publish/ {at?} (a future `at` schedules it), or unpublish/."""

    permission_classes = [IsAdminUser]
    publish = True

    def post(self, request: Request, pk: int) -> Response:
        article = get_object_or_404(Article, pk=pk)
        data = PublishInputSerializer(data=request.data or {})
        data.is_valid(raise_exception=True)
        try:
            if self.publish:
                article = services.publish(article, at=data.validated_data.get("at"))
                newsletter.on_published(article)
            else:
                article = services.unpublish(article)
        except services.BlogError as exc:
            return _refused(exc)
        _audit(request, "published" if self.publish else "unpublished", article)
        return Response(StaffArticleSerializer(article).data)


class StaffUnpublishView(StaffPublishView):
    publish = False


class StaffImageUploadView(APIView):
    """POST /api/admin/blog/images/ (multipart `file`) -> {id, url}. PNG/JPG/WebP/GIF, up to 5 MB."""

    permission_classes = [IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request: Request) -> Response:
        upload = request.FILES.get("file")
        if upload is None:
            return Response({"detail": "Choose an image to upload."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            img = services.save_image(upload, request.user)
        except services.BlogError as exc:
            return _refused(exc)
        return Response(image_payload(img), status=status.HTTP_201_CREATED)
