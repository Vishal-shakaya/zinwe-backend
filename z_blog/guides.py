"""
Free e-guides (PDF) at /blog/guides: a public page per guide (cover, contents, sample pages) and a download
for members of the free membership — the newsletter list that also opens the series. One email unlocks every
guide. A member is recognised by their reader key (`?k=`, z_blog.series.reader); a visitor signs up on the
guide's page and the guide downloads the moment they confirm (the website's /newsletter page does that).
"""
from __future__ import annotations

import re
from typing import Any

from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.db.models import F, QuerySet
from django.utils.text import slugify

from .models import Article, BlogImage, Guide, GuideDownload, NewsletterSubscriber
from .services import BlogError

MAX_PDF_BYTES = 9 * 1024 * 1024  # under nginx's 10 MB request limit
GUIDE_PAGE = re.compile(r"^/blog/guides/([-a-zA-Z0-9_]+)$")


def live() -> QuerySet[Guide]:
    return Guide.objects.filter(status=Guide.Status.PUBLISHED).select_related("cover_image")


def path(guide: Guide) -> str:
    return f"/blog/guides/{guide.slug}"


def from_source(source: str) -> Guide | None:
    """The live guide whose page a sign-up came from (its `source`), if any."""
    m = GUIDE_PAGE.match((source or "").split("?", 1)[0].rstrip("/"))
    return live().filter(slug=m[1]).first() if m else None


def campaign(guide: Guide) -> str:
    return guide.campaign or f"guide-{guide.slug[:18]}"


def samples(guide: Guide) -> list[BlogImage]:
    ids = [i for i in (guide.sample_pages or []) if isinstance(i, int)]
    found = {img.id: img for img in BlogImage.objects.filter(id__in=ids)}
    return [found[i] for i in ids if i in found]


def record_download(guide: Guide, sub: NewsletterSubscriber) -> None:
    if not GuideDownload.objects.filter(guide=guide, subscriber=sub).update(downloads=F("downloads") + 1):
        GuideDownload.objects.get_or_create(guide=guide, subscriber=sub)


# --- Matching (blog articles, series episodes) ------------------------------------------

_STOP = {
    "the", "and", "for", "with", "your", "you", "that", "this", "from", "what", "when", "how", "why", "who",
    "small", "business", "businesses", "zinwe", "guide", "into", "about", "their", "last", "file", "series",
}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", (text or "").lower()) if w not in _STOP}


def best_for(tags: str, text: str) -> Guide | None:
    """The live guide closest to a piece of content (tags count double); the first guide when nothing matches."""
    want = _words(tags) | _words(text)
    best, best_score, first = None, 0, None
    for g in live():
        first = first or g
        score = 2 * len(want & _words(g.tags)) + len(want & _words(f"{g.title} {g.subtitle} {g.summary}"))
        if score > best_score:
            best, best_score = g, score
    return best or first


def for_article(article: Article) -> Guide | None:
    return best_for(article.tags, f"{article.title} {article.summary}")


def card(g: Guide) -> dict[str, Any]:
    return {
        "slug": g.slug, "title": g.title, "subtitle": g.subtitle, "summary": g.summary, "audience": g.audience,
        "cover": {"id": g.cover_image.id, "url": g.cover_image.url} if g.cover_image else None,
        "cover_alt": g.cover_alt, "page_count": g.page_count, "highlights": g.highlights or [],
        "campaign": campaign(g),
    }


# --- Staff ------------------------------------------------------------------------------

EDITABLE = (
    "title", "subtitle", "summary", "description", "audience", "cover_image", "cover_alt", "sample_pages", "contents",
    "highlights", "feature_line", "tags", "campaign", "status", "position", "filename",
)


def unique_slug(text: str, exclude_id: int | None = None) -> str:
    base = slugify(text)[:150] or "guide"
    slug, n = base, 2
    taken = Guide.objects.exclude(pk=exclude_id) if exclude_id else Guide.objects.all()
    while taken.filter(slug=slug).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


def _clean_list(raw: Any, keys: tuple[str, ...], limit: int) -> list[dict[str, str]]:
    rows = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            row = {k: str(item.get(k) or "").strip()[:160] for k in keys}
            if any(row.values()):
                rows.append(row)
    return rows[:limit]


@transaction.atomic
def save(guide: Guide | None, data: dict[str, Any]) -> Guide:
    guide = guide or Guide()
    for name in EDITABLE:
        if name in data:
            setattr(guide, name, data[name])
    guide.title = re.sub(r"\s+", " ", guide.title or "").strip()
    if not guide.title:
        raise BlogError("Give the guide a title.")
    guide.contents = _clean_list(guide.contents, ("title", "note"), 30)
    guide.highlights = _clean_list(guide.highlights, ("value", "label"), 6)
    guide.sample_pages = [int(i) for i in (guide.sample_pages or []) if str(i).isdigit()][:8]
    guide.campaign = re.sub(r"[^a-z0-9-]", "", (guide.campaign or "").lower())[:24]
    if guide.status == Guide.Status.PUBLISHED and not guide.file:
        raise BlogError("Upload the guide's PDF before publishing it.")
    wanted = data.get("slug") or (guide.slug if guide.pk else "") or guide.title
    guide.slug = unique_slug(wanted, exclude_id=guide.pk)
    guide.save()
    return guide


def attach_pdf(guide: Guide, upload: UploadedFile) -> Guide:
    if upload.size > MAX_PDF_BYTES:
        raise BlogError("That PDF is over 9 MB. Export a smaller one (compress the images).")
    head = upload.read(8)
    upload.seek(0)
    if not head.startswith(b"%PDF-"):
        raise BlogError("Upload a PDF file.")
    data = upload.read()
    upload.seek(0)
    pages = len(re.findall(rb"/Type\s*/Page[^s]", data)) or guide.page_count
    if guide.file:
        guide.file.delete(save=False)
    guide.file.save("guide.pdf", upload, save=False)
    guide.size = upload.size
    guide.page_count = pages
    if not guide.filename:
        guide.filename = f"zinwe-{guide.slug or 'guide'}.pdf"[:120]
    guide.save()
    return guide
