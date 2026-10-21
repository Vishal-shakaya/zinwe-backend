"""Blog rules: cleaning rich text, slugs, reading time, publishing, images, and public queries."""
from __future__ import annotations

import html as html_lib
import math
import re
from datetime import datetime
from typing import Any

import nh3
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.db.models import Count, QuerySet
from django.utils import timezone
from django.utils.text import slugify

from .models import Article, BlogImage

Status = Article.Status

# What the article editor produces (Quill semantic HTML) plus images, and nothing else:
# no scripts, styles, iframes or event handlers. Links to http(s)/mailto; images to http(s) or
# our own uploads (/api/blog/images/...).
BODY_TAGS = {
    "p", "br", "hr", "strong", "b", "em", "i", "u", "s", "a", "ul", "ol", "li",
    "h2", "h3", "h4", "blockquote", "code", "pre", "img", "figure", "figcaption",
}
BODY_ATTRIBUTES = {"a": {"href", "title"}, "img": {"src", "alt", "width", "height"}}

IMAGE_TYPES = {
    # content type -> (extension, accepted leading bytes)
    "image/png": (".png", (b"\x89PNG\r\n\x1a\n",)),
    "image/jpeg": (".jpg", (b"\xff\xd8\xff",)),
    "image/webp": (".webp", (b"RIFF",)),
    "image/gif": (".gif", (b"GIF87a", b"GIF89a")),
}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
WORDS_PER_MINUTE = 200


class BlogError(Exception):
    """A refused blog action; str(exc) is shown to the operator."""


# --- Content ------------------------------------------------------------------


def clean_body(raw: str) -> str:
    """Allow-list clean of the article body (staff-written, published to the public site)."""
    cleaned = nh3.clean(
        raw or "", tags=BODY_TAGS, attributes=BODY_ATTRIBUTES,
        url_schemes={"http", "https", "mailto"}, link_rel="noopener noreferrer",
    )
    # Quill writes every space as &nbsp; -- that stops text wrapping on the site.
    return cleaned.replace("&nbsp;", " ").replace(" ", " ").strip()


def plain_text(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def reading_minutes(html: str) -> int:
    words = len(plain_text(html).split())
    return max(1, math.ceil(words / WORDS_PER_MINUTE))


# /blog/<slug> URLs the site itself uses.
RESERVED_SLUGS = {"series"}


def unique_slug(text: str, exclude_id: int | None = None) -> str:
    base = slugify(text)[:200] or "article"
    if base in RESERVED_SLUGS:
        base = f"{base}-article"
    slug, n = base, 2
    taken = Article.objects.exclude(pk=exclude_id) if exclude_id else Article.objects.all()
    while taken.filter(slug=slug).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


def normalise_tags(raw: str) -> str:
    seen: list[str] = []
    for tag in (raw or "").split(","):
        t = re.sub(r"\s+", " ", tag).strip().lower()[:40]
        if t and t not in seen:
            seen.append(t)
    return ", ".join(seen)[:200]


EDITABLE = (
    "title", "type", "summary", "body_html", "cover_image", "cover_alt", "tags",
    "seo_title", "meta_description", "author_name",
    "series", "season", "episode", "teaser_html", "free_preview", "newsletter_subject",
)


@transaction.atomic
def save_article(article: Article | None, data: dict[str, Any], staff: Any) -> Article:
    """Create or update an article from validated editor data (slug is kept unique)."""
    article = article or Article(author=staff)
    for field in EDITABLE:
        if field in data:
            setattr(article, field, data[field])
    article.title = re.sub(r"\s+", " ", article.title or "").strip()
    if not article.title:
        raise BlogError("Give the article a title.")
    article.body_html = clean_body(article.body_html)
    article.teaser_html = clean_body(article.teaser_html)
    article.tags = normalise_tags(article.tags)
    article.reading_minutes = reading_minutes(f"{article.teaser_html} {article.body_html}")
    from .series import check_episode  # series.py builds on this module

    check_episode(article)
    if not article.author_name and staff is not None:
        article.author_name = getattr(staff, "full_name", "") or "Zinwe team"
    wanted = data.get("slug") or (article.slug if article.pk else "") or article.title
    article.slug = unique_slug(wanted, exclude_id=article.pk)
    article.save()
    return article


def publish(article: Article, at: datetime | None = None) -> Article:
    """Publish now (keeping the first publish date), or at `at`: a future time schedules it.

    A scheduled article is stored as published with a future `published_at`; `published()` hides
    it until then, so it goes live on its own with no job needed.
    """
    if not plain_text(article.body_html):
        raise BlogError("Write the article before publishing it.")
    from .series import episode_ready

    episode_ready(article)
    article.status = Status.PUBLISHED
    article.published_at = at or article.published_at or timezone.now()
    article.save(update_fields=["status", "published_at", "updated_at"])
    return article


def state(article: Article, now: datetime | None = None) -> str:
    """draft | scheduled | published (live on the website)."""
    if article.status != Status.PUBLISHED or article.published_at is None:
        return "draft"
    return "scheduled" if article.published_at > (now or timezone.now()) else "published"


def is_live(article: Article) -> bool:
    return state(article) == "published"


def unpublish(article: Article) -> Article:
    article.status = Status.DRAFT
    article.save(update_fields=["status", "updated_at"])
    return article


# --- Images -------------------------------------------------------------------


def save_image(upload: UploadedFile, staff: Any) -> BlogImage:
    if upload.size > MAX_IMAGE_BYTES:
        raise BlogError("That image is over 5 MB. Use a smaller one.")
    head = upload.read(16)
    upload.seek(0)
    for content_type, (ext, signatures) in IMAGE_TYPES.items():
        if any(head.startswith(sig) for sig in signatures) and (content_type != "image/webp" or b"WEBP" in head):
            upload.name = f"upload{ext}"
            return BlogImage.objects.create(
                file=upload, content_type=content_type, size=upload.size, uploaded_by=staff
            )
    raise BlogError("Upload a PNG, JPG, WebP or GIF image.")


# --- Public queries -----------------------------------------------------------


def published() -> QuerySet[Article]:
    """Live blog articles. Series episodes live on their series page (z_blog.series), not in the blog list."""
    return (
        Article.objects.filter(
            status=Status.PUBLISHED, published_at__isnull=False, published_at__lte=timezone.now(), series__isnull=True
        )
        .select_related("cover_image")
        .order_by("-published_at", "-id")
    )


def type_counts() -> list[dict[str, Any]]:
    counts = dict(published().values_list("type").annotate(n=Count("id")).order_by())
    return [{"key": k, "label": label, "count": int(counts.get(k, 0))} for k, label in Article.Type.choices]


def related(article: Article, limit: int = 3) -> list[Article]:
    """Same type first, then the latest of any type."""
    same = list(published().filter(type=article.type).exclude(pk=article.pk)[:limit])
    if len(same) < limit:
        more = published().exclude(pk__in=[article.pk, *[a.pk for a in same]])[: limit - len(same)]
        same.extend(more)
    return same
