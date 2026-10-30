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
from .seo_meta import modified_at


def entries() -> list[tuple[str, datetime]]:
    """(path, last modified) for every public blog page, index pages first. A page's last-modified time is its last
    edit or its publish time, whichever is later (a scheduled article was last saved before it went live); an index
    page's is the newest of what it lists."""
    out: list[tuple[str, datetime]] = []
    articles = [(a, modified_at(a)) for a in services.published()]
    out.append(("/blog", max((when for _, when in articles), default=timezone.now())))
    out += [(f"/blog/{a.slug}", when) for a, when in articles]

    shows = []
    for show in series.live_series():
        episodes = [(a, modified_at(a)) for a in series.lineup(show).released]
        shows.append((show, episodes, max([show.updated_at, *(when for _, when in episodes)])))
    if shows:
        out.append(("/blog/series", max(when for _, _, when in shows)))
    for show, episodes, when in shows:
        out.append((f"/blog/series/{show.slug}", when))
        out += [(series.path(a), ep_when) for a, ep_when in episodes]

    books = list(guides.live())
    if books:
        out.append(("/blog/guides", max(g.updated_at for g in books)))
        out += [(guides.path(g), g.updated_at) for g in books]
    return out
