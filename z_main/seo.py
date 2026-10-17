"""
robots.txt and the sitemaps for the website, at the site root. nginx sends /robots.txt, /sitemap.xml and
/sitemap-pages.xml here on the portal domain (and /robots.txt on the admin and API domains); locally the Vite
dev server proxies them.

- robots.txt: on the production portal host (ZINWE_SEARCH_INDEXING), public pages are open and the signed-in
  app, onboarding, personal links and the API are not. Everywhere else (local, dev, the admin and API hosts)
  everything is closed.
- sitemap.xml: a sitemap index -> the website's pages (sitemap-pages.xml) and, once an article is published,
  the blog's own sitemap (/api/blog/sitemap.xml).
- sitemap-pages.xml: the file the frontend build writes next to index.html (build/seoPages.ts), with its URLs
  moved onto this environment's own address.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseNotFound
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_GET

from z_blog import services as blog

log = logging.getLogger(__name__)

# The address the frontend build writes into sitemap-pages.xml (src/content/seo.ts SITE_URL).
BUILD_SITE_URL = "https://zinwe.io"
SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"

# Kept out of search: the signed-in app and onboarding, personal links (shared quotes) and the API, except the
# blog's sitemap, feed and images. Sign-in, sign-up and newsletter pages stay crawlable on purpose: they carry a
# noindex tag, which search engines can only read if they're allowed to fetch the page.
ALLOW = ("/api/blog/",)
DISALLOW = ("/app", "/onboarding", "/quote/", "/api/")


def _host(url_or_host: str) -> str:
    netloc = urlsplit(url_or_host).netloc if "//" in url_or_host else url_or_host
    return netloc.split(":")[0].lower()


def is_indexable(request: HttpRequest) -> bool:
    """Search engines welcome: production only, and only on the website's own address."""
    return settings.ZINWE_SEARCH_INDEXING and _host(request.get_host()) == _host(settings.ZINWE_APP_URL)


def robots_body(indexable: bool) -> str:
    if not indexable:
        return "# Not for search engines: a test, admin or API address.\nUser-agent: *\nDisallow: /\n"
    lines = [
        f"# {_host(settings.ZINWE_APP_URL)}: public pages are open to search engines;",
        "# the app, personal links and the API are not.",
        "User-agent: *",
        *(f"Allow: {path}" for path in ALLOW),
        *(f"Disallow: {path}" for path in DISALLOW),
        "",
        f"Sitemap: {settings.ZINWE_APP_URL}/sitemap.xml",
    ]
    return "\n".join(lines) + "\n"


def pages_file() -> Path:
    """The frontend build's page sitemap, next to the index.html Django already serves for /blog."""
    return Path(settings.ZINWE_FRONTEND_INDEX).with_name("sitemap-pages.xml")


@require_GET
@cache_control(public=True, max_age=3600)
def robots_txt(request: HttpRequest) -> HttpResponse:
    """GET /robots.txt"""
    return HttpResponse(robots_body(is_indexable(request)), content_type="text/plain; charset=utf-8")


@require_GET
@cache_control(public=True, max_age=3600)
def sitemap_index(request: HttpRequest) -> HttpResponse:
    """GET /sitemap.xml -- the website's pages, plus the blog once it has an article."""
    base = settings.ZINWE_APP_URL
    entries: list[tuple[str, datetime]] = []
    pages = pages_file()
    if pages.is_file():
        entries.append((f"{base}/sitemap-pages.xml", datetime.fromtimestamp(pages.stat().st_mtime, tz=dt_timezone.utc)))
    else:
        log.warning("sitemap: %s is missing (frontend not built?)", pages)
    latest = blog.published().order_by("-updated_at").values_list("updated_at", flat=True).first()
    if latest is not None:
        entries.append((f"{base}/api/blog/sitemap.xml", latest))
    body = "".join(
        f"<sitemap><loc>{escape(loc)}</loc><lastmod>{when:%Y-%m-%d}</lastmod></sitemap>" for loc, when in entries
    )
    xml = f'<?xml version="1.0" encoding="UTF-8"?>\n<sitemapindex xmlns="{SITEMAP_NS}">{body}</sitemapindex>\n'
    return HttpResponse(xml, content_type="application/xml")


@require_GET
@cache_control(public=True, max_age=3600)
def sitemap_pages(request: HttpRequest) -> HttpResponse:
    """GET /sitemap-pages.xml -- the frontend build's page list, on this environment's address."""
    try:
        xml = pages_file().read_text(encoding="utf-8")
    except OSError:
        log.warning("sitemap: %s is missing (frontend not built?)", pages_file())
        return HttpResponseNotFound("Not built yet.", content_type="text/plain")
    if settings.ZINWE_APP_URL != BUILD_SITE_URL:
        xml = xml.replace(f"<loc>{BUILD_SITE_URL}", f"<loc>{settings.ZINWE_APP_URL}")
    return HttpResponse(xml, content_type="application/xml")
