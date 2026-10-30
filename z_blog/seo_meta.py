"""
Search-result text and image sizes for the public blog pages: one place for the rules the pre-render
(z_blog/prerender.py) and the public API share, so the HTML robots read and the page the app renders agree.

- page_title(): "<topic> | Zinwe" (the website's one separator style); the brand is dropped, never words cut,
  when that would pass 60 characters.
- describe(): a 140-160 character description from the summary (and the body when the summary is short),
  cut at a sentence or word boundary.
- image_size() / sized_images(): the stored image's real width and height (read from the file header, cached by
  the image's key: an uploaded image never changes), so <img> tags reserve their space before they load.
"""
from __future__ import annotations

import html as html_lib
import logging
import re
from datetime import datetime
from typing import Iterable

from .models import BlogImage

logger = logging.getLogger("zinwe.blog.seo")

BRAND = "Zinwe"
SEPARATOR = " | "
TITLE_MAX = 60
DESCRIPTION_MIN = 140
DESCRIPTION_MAX = 160
HEADLINE_MAX = 110  # schema.org Article headline (Google's limit)

# The house standard for covers (and what an unreadable cover is assumed to be).
COVER_SIZE = (1200, 630)

# A brand suffix someone already typed into an SEO title ("Stop USB leaks | Zinwe", "... — Zinwe").
_BRAND_SUFFIX = re.compile(rf"\s*[|—–:-]\s*{BRAND}\s*$", re.I)


def page_title(*parts: str) -> str:
    """'<part> | <part> | Zinwe', dropping the brand (then the extra parts) rather than cutting words past 60."""
    clean = [_BRAND_SUFFIX.sub("", p).strip() for p in parts if p and p.strip()]
    clean = [p for p in clean if p] or [BRAND]
    for candidate in (
        [*clean, BRAND],
        clean,
        [clean[0], BRAND],
    ):
        text = SEPARATOR.join(dict.fromkeys(candidate))  # never "Zinwe | Zinwe"
        if len(text) <= TITLE_MAX:
            return text
    return clean[0]


def _plain(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def clip(text: str, limit: int) -> str:
    """At most `limit` characters, cut at a word boundary (with an ellipsis when cut)."""
    text = _plain(text)
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if text[limit - 1] != " " and " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-–—") + "…"


def describe(*texts: str, low: int = DESCRIPTION_MIN, high: int = DESCRIPTION_MAX) -> str:
    """A search description of `low`-`high` characters from the given texts (summary first, then the body): whole
    sentences when they fit the range, else cut at a word boundary. Shorter only when there isn't more to say."""
    text = " ".join(t for t in (_plain(x) for x in texts) if t)
    if len(text) <= high:
        return text
    out = ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        joined = f"{out} {sentence}".strip()
        if len(joined) > high:
            break
        out = joined
    return out if len(out) >= low else clip(text, high)


def article_description(a: object) -> str:
    """An article's search description: its own, else a 140-160 character one from the summary and then the body
    (an episode's public opening, for a series episode: its full text is for members)."""
    own = (getattr(a, "meta_description", "") or "").strip()
    if own:
        return own
    body = getattr(a, "teaser_html", "") if getattr(a, "series_id", None) else getattr(a, "body_html", "")
    return describe(getattr(a, "summary", ""), body)


def headline(text: str) -> str:
    return clip(text, HEADLINE_MAX)


def modified_at(obj: object) -> datetime:
    """When a public page last changed: its last edit, or its publish time if later (a scheduled article is saved
    before it goes live, so its `updated_at` is older than its `published_at`)."""
    times = [t for t in (getattr(obj, "updated_at", None), getattr(obj, "published_at", None)) if t]
    return max(times)


# --- Image sizes -------------------------------------------------------------------------------------------

_sizes: dict[str, tuple[int, int] | None] = {}
_SIZES_MAX = 4096


def image_size(img: BlogImage | None) -> tuple[int, int] | None:
    """(width, height) of an uploaded image, from its file header; None if it can't be read."""
    if img is None:
        return None
    key = str(img.key)
    if key in _sizes:
        return _sizes[key]
    size = None
    try:
        from PIL import Image

        with img.file.open("rb") as f, Image.open(f) as im:
            size = (int(im.width), int(im.height))
    except Exception:  # missing file, unknown format, no Pillow: the caller falls back
        logger.info("blog image %s: size unknown", key)
    if len(_sizes) >= _SIZES_MAX:
        _sizes.clear()
    _sizes[key] = size
    return size


def cover_size(img: BlogImage | None) -> tuple[int, int]:
    """A cover's real size, else the house standard 1200 x 630."""
    return image_size(img) or COVER_SIZE


_IMG_TAG = re.compile(r"<img\b([^>]*?)\s*/?>", re.I)
_IMAGE_KEY = re.compile(r"/api/blog/images/([0-9a-fA-F-]{32,36})/")
_SRC = re.compile(r'\bsrc="([^"]*)"', re.I)


def _has(attrs: str, name: str) -> bool:
    return re.search(rf"\b{name}\s*=", attrs, re.I) is not None


def _sizes_for(keys: Iterable[str]) -> dict[str, tuple[int, int]]:
    wanted = {k.lower().replace("-", "") for k in keys}
    if not wanted:
        return {}
    found: dict[str, tuple[int, int]] = {}
    for img in BlogImage.objects.filter(key__in=list(wanted)):
        size = image_size(img)
        if size:
            found[img.key.hex] = size
    return found


def sized_images(body_html: str) -> str:
    """In-body <img> tags (allow-list cleaned HTML) with width/height when the image is one of ours, plus
    loading="lazy" and decoding="async": they sit below the cover, so they never hold up the first paint."""
    if "<img" not in (body_html or ""):
        return body_html or ""
    keys = _IMAGE_KEY.findall(body_html)
    try:
        sizes = _sizes_for(keys)
    except Exception:  # a malformed key never breaks the page
        sizes = {}

    def fix(m: re.Match[str]) -> str:
        attrs = m[1]
        src = _SRC.search(attrs)
        key = _IMAGE_KEY.search(src[1]) if src else None
        size = sizes.get(key[1].lower().replace("-", "")) if key else None
        extra = []
        if size and not _has(attrs, "width") and not _has(attrs, "height"):
            extra.append(f'width="{size[0]}" height="{size[1]}"')
        if not _has(attrs, "loading"):
            extra.append('loading="lazy"')
        if not _has(attrs, "decoding"):
            extra.append('decoding="async"')
        return f"<img{attrs}{' ' + ' '.join(extra) if extra else ''}>"

    return _IMG_TAG.sub(fix, body_html)
