"""
Members-only newsletter series (THE LAST FILE): the series pages at /blog/series/<slug>, their episodes,
and who may read them.

An episode is an `Article` with `series` set. Everyone sees its card, cover and public opening
(`teaser_html`); the full text (`body_html`) and its files are for confirmed newsletter subscribers,
recognised by their reader key (`NewsletterSubscriber.access_key`, sent as `?k=`). The key reaches the
reader's browser from the confirmation link, and from the links in every episode email. Membership is
free: the subscription *is* the membership.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.db.models import Max, QuerySet
from django.utils import timezone
from django.utils.text import slugify

from .models import Article, Attachment, NewsletterSubscriber, Series
from .services import BlogError, plain_text

Status = Article.Status

# What an episode may come with: PDFs (the case file), images, and zipped resource packs.
FILE_TYPES = {
    # content type -> (extension, accepted leading bytes)
    "application/pdf": (".pdf", (b"%PDF-",)),
    "image/png": (".png", (b"\x89PNG\r\n\x1a\n",)),
    "image/jpeg": (".jpg", (b"\xff\xd8\xff",)),
    "application/zip": (".zip", (b"PK\x03\x04",)),
}
MAX_FILE_BYTES = 9 * 1024 * 1024  # under nginx's 10 MB request limit


# --- Readers ----------------------------------------------------------------------


def reader(key: str | None) -> NewsletterSubscriber | None:
    """The confirmed subscriber this reader key belongs to (None for a missing, wrong or cancelled one)."""
    key = (key or "").strip()
    if not key or len(key) > 64:
        return None
    return NewsletterSubscriber.objects.filter(access_key=key, status=NewsletterSubscriber.Status.ACTIVE).first()


def can_read(article: Article, sub: NewsletterSubscriber | None) -> bool:
    return article.series_id is None or article.free_preview or sub is not None


def masked(email: str) -> str:
    """v•••@gmail.com: enough for "reading as ..." without showing the whole address on screen."""
    name, _, domain = email.partition("@")
    return f"{name[:1]}•••@{domain}" if domain else "•••"


# --- Public queries ---------------------------------------------------------------


def live_series() -> QuerySet[Series]:
    return Series.objects.filter(status=Series.Status.PUBLISHED).select_related("cover_image")


def _scheduled_or_live(series: Series) -> QuerySet[Article]:
    return (
        Article.objects.filter(series=series, status=Status.PUBLISHED, published_at__isnull=False)
        .select_related("cover_image", "series")
        .order_by("season", "episode", "published_at", "id")
    )


@dataclass
class Lineup:
    released: list[Article] = field(default_factory=list)
    upcoming: list[Article] = field(default_factory=list)  # scheduled: shown as "coming <date>"


def lineup(series: Series, now: datetime | None = None) -> Lineup:
    now = now or timezone.now()
    out = Lineup()
    for a in _scheduled_or_live(series).prefetch_related("attachments"):
        (out.released if a.published_at <= now else out.upcoming).append(a)
    return out


def released_episode(series_slug: str, episode_slug: str) -> Article | None:
    """A live episode of a live series."""
    return (
        Article.objects.filter(
            series__slug=series_slug, series__status=Series.Status.PUBLISHED, slug=episode_slug,
            status=Status.PUBLISHED, published_at__isnull=False, published_at__lte=timezone.now(),
        )
        .select_related("cover_image", "series", "series__cover_image")
        .prefetch_related("attachments")
        .first()
    )


def neighbours(article: Article) -> tuple[Article | None, Article | None]:
    """The released episodes just before and after this one (by season, then number)."""
    episodes = lineup(article.series).released
    ids = [a.pk for a in episodes]
    i = ids.index(article.pk) if article.pk in ids else -1
    if i < 0:
        return None, None
    return (episodes[i - 1] if i > 0 else None), (episodes[i + 1] if i + 1 < len(episodes) else None)


def path(article: Article) -> str:
    return f"/blog/series/{article.series.slug}/{article.slug}"


def label(article: Article) -> str:
    """THE LAST FILE · EPISODE 01"""
    number = f" · EPISODE {article.episode:02d}" if article.episode else ""
    return f"{article.series.title.upper()}{number}"


def seasons(series: Series, episodes: list[Article]) -> list[dict[str, Any]]:
    """The series' configured seasons, plus any season an episode uses that isn't configured."""
    found: dict[int, dict[str, Any]] = {}
    for s in series.seasons or []:
        try:
            n = int(s.get("number"))
        except (TypeError, ValueError, AttributeError):
            continue
        found[n] = {"number": n, "title": str(s.get("title") or "")[:80], "theme": str(s.get("theme") or "")[:80]}
    for a in episodes:
        found.setdefault(a.season, {"number": a.season, "title": "", "theme": ""})
    return [found[n] for n in sorted(found)]


# --- Staff ------------------------------------------------------------------------

SEASON_LINE = re.compile(r"^\s*(\d{1,2})\s*[|:.\-–]\s*([^|]*?)\s*(?:\|\s*(.*?)\s*)?$")


def parse_seasons(raw: Any) -> list[dict[str, Any]]:
    """Seasons from the editor: a list of {number, title, theme}, or text lines "1 | The Warning | Trust"."""
    rows: list[dict[str, Any]] = []
    if isinstance(raw, str):
        for line in raw.splitlines():
            m = SEASON_LINE.match(line)
            if m:
                rows.append({"number": int(m[1]), "title": m[2][:80], "theme": (m[3] or "")[:80]})
    elif isinstance(raw, list):
        for s in raw:
            if isinstance(s, dict) and str(s.get("number", "")).isdigit():
                rows.append({
                    "number": int(s["number"]), "title": str(s.get("title") or "")[:80],
                    "theme": str(s.get("theme") or "")[:80],
                })
    seen: dict[int, dict[str, Any]] = {}
    for r in rows:
        if 1 <= r["number"] <= 99:
            seen[r["number"]] = r
    return [seen[n] for n in sorted(seen)]


def unique_series_slug(text: str, exclude_id: int | None = None) -> str:
    base = slugify(text)[:120] or "series"
    slug, n = base, 2
    taken = Series.objects.exclude(pk=exclude_id) if exclude_id else Series.objects.all()
    while taken.filter(slug=slug).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


SERIES_EDITABLE = ("title", "tagline", "description", "cover_image", "cover_alt", "planned_episodes", "cadence", "position")


@transaction.atomic
def save_series(series: Series | None, data: dict[str, Any]) -> Series:
    series = series or Series()
    for name in SERIES_EDITABLE:
        if name in data:
            setattr(series, name, data[name])
    if "seasons" in data:
        series.seasons = parse_seasons(data["seasons"])
    if "status" in data and data["status"] in Series.Status.values:
        series.status = data["status"]
    series.title = re.sub(r"\s+", " ", series.title or "").strip()
    if not series.title:
        raise BlogError("Give the series a title.")
    wanted = data.get("slug") or (series.slug if series.pk else "") or series.title
    series.slug = unique_series_slug(wanted, exclude_id=series.pk)
    series.save()
    return series


def next_episode_number(series: Series, exclude_id: int | None = None) -> int:
    qs = Article.objects.filter(series=series)
    if exclude_id:
        qs = qs.exclude(pk=exclude_id)
    return (qs.aggregate(n=Max("episode"))["n"] or 0) + 1


def check_episode(article: Article) -> None:
    """Called before an episode is saved: a free number, and series fields cleared on ordinary articles."""
    if article.series_id is None:
        article.episode = None
        article.free_preview = False
        return
    if not article.episode:
        article.episode = next_episode_number(article.series, exclude_id=article.pk)
    clash = Article.objects.filter(series_id=article.series_id, episode=article.episode).exclude(pk=article.pk).first()
    if clash is not None:
        raise BlogError(f"Episode {article.episode} of this series is already “{clash.title}”. Pick another number.")


def save_attachment(article: Article, upload: UploadedFile, title: str) -> Attachment:
    if upload.size > MAX_FILE_BYTES:
        raise BlogError("That file is over 9 MB. Use a smaller one.")
    head = upload.read(16)
    upload.seek(0)
    original = (upload.name or "file").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    for content_type, (ext, signatures) in FILE_TYPES.items():
        if any(head.startswith(sig) for sig in signatures):
            stem = slugify(original.rsplit(".", 1)[0])[:90] or "zinwe-file"
            upload.name = f"upload{ext}"
            position = (article.attachments.aggregate(n=Max("position"))["n"] or 0) + 1
            return Attachment.objects.create(
                article=article, file=upload, title=(title or "").strip()[:120] or original[:120],
                filename=f"{stem}{ext}", content_type=content_type, size=upload.size, position=position,
            )
    raise BlogError("Upload a PDF, PNG, JPG or ZIP file.")


def file_access(att: Attachment, sub: NewsletterSubscriber | None) -> str:
    """"ok", "members" (a members-only file and no valid reader key) or "missing" (its article isn't live)."""
    a = att.article
    live = a.status == Status.PUBLISHED and a.published_at is not None and a.published_at <= timezone.now()
    if not live or (a.series_id is not None and a.series.status != Series.Status.PUBLISHED):
        return "missing"
    return "ok" if can_read(a, sub) else "members"


def episode_ready(article: Article) -> None:
    """Refuse to publish an episode with nothing members can read."""
    if article.series_id is not None and not plain_text(article.teaser_html):
        raise BlogError("Write the public opening (the teaser everyone sees) before publishing the episode.")
