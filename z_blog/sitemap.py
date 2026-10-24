"""
Every public blog URL for search engines (FIX-010): the blog index, published articles, live series (the hub and
each series page), their released episodes, and the guides library and each published guide.

Used by the site's /sitemap.xml (z_main/seo.py, the one robots.txt names) and /api/blog/sitemap.xml. Paths are the
website's own /blog addresses, which nginx sends to Django's pre-rendered pages (z_blog/site_urls.py), without a
trailing slash (the canonical form). Drafts, scheduled episodes, unpublished series and guides, and 404 / noindex
pages are never listed.
"""
from __future__ import annotations

from datetime import datetime

from django.utils import timezone

from . import guides, series, services


def entries() -> list[tuple[str, datetime]]:
    """(path, last modified) for every public blog page, index pages first."""
    out: list[tuple[str, datetime]] = []
    articles = list(services.published())
    # The index changes when an article does (not on every request), so its lastmod is the newest article's.
    out.append(("/blog", max((a.updated_at for a in articles), default=timezone.now())))
    out += [(f"/blog/{a.slug}", a.updated_at) for a in articles]

    shows = [(show, series.lineup(show).released) for show in series.live_series()]
    if shows:
        out.append(("/blog/series", max(max([s.updated_at, *(a.updated_at for a in eps)]) for s, eps in shows)))
    for show, episodes in shows:
        out.append((f"/blog/series/{show.slug}", max([show.updated_at, *(a.updated_at for a in episodes)])))
        out += [(series.path(a), a.updated_at) for a in episodes]

    books = list(guides.live())
    if books:
        out.append(("/blog/guides", max(g.updated_at for g in books)))
        out += [(guides.path(g), g.updated_at) for g in books]
    return out
