"""
robots.txt and the sitemaps for the website, at the site root. nginx sends /robots.txt, /sitemap.xml and
/sitemap-pages.xml here on the portal domain (and /robots.txt on the admin and API domains); locally the Vite
dev server proxies them.

- robots.txt: on the production portal host (ZINWE_SEARCH_INDEXING), public pages are open and the signed-in
  app, onboarding, personal links and the API are not. Everywhere else (local, dev, the admin and API hosts)
  everything is closed.
- sitemap.xml: ONE list of every public URL: all the website's pages (from the frontend build's
  sitemap-pages.xml, which build/seoPages.ts checks against the app's routes) plus every published blog
  article. /blog's lastmod is the newest article's when that's later than the build.
- sitemap-pages.xml: the file the frontend build writes next to index.html (build/seoPages.ts), with its URLs
  moved onto this environment's own address (kept for reference; sitemap.xml already includes it).
"""
from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit
from xml.sax.saxutils import escape, unescape

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


# One <url> of the build's sitemap: our own generated file, so a plain pattern is enough (no XML parser).
_URL_RE = re.compile(r"<url>\s*<loc>([^<]+)</loc>(?:\s*<lastmod>([^<]+)</lastmod>)?", re.S)


def frontend_pages() -> list[tuple[str, date | None]]:
    """(URL on this environment's address, lastmod) for every page in the frontend build's sitemap."""
    try:
        xml = pages_file().read_text(encoding="utf-8")
    except OSError:
        log.warning("sitemap: %s is missing (frontend not built?)", pages_file())
        return []
    pages: list[tuple[str, date | None]] = []
    for loc, lastmod in _URL_RE.findall(xml):
        url = unescape(loc.strip())
        if url.startswith(BUILD_SITE_URL):
            url = settings.ZINWE_APP_URL + url[len(BUILD_SITE_URL):]
        try:
            when = date.fromisoformat(lastmod.strip()[:10]) if lastmod else None
        except ValueError:
            when = None
        pages.append((url, when))
    return pages


def sitemap_urls() -> list[tuple[str, date | None]]:
    """Every public URL: the website's pages, then the published blog articles (newest first)."""
    base = settings.ZINWE_APP_URL
    urls: dict[str, date | None] = dict(frontend_pages())
    articles = list(blog.published().order_by("-updated_at"))
    if articles:
        newest = articles[0].updated_at.date()
        index = f"{base}/blog"
        urls[index] = max(filter(None, [urls.get(index), newest]))
        for article in articles:
            urls[f"{base}/blog/{article.slug}"] = article.updated_at.date()
    return list(urls.items())


@require_GET
@cache_control(public=True, max_age=3600)
def robots_txt(request: HttpRequest) -> HttpResponse:
    """GET /robots.txt"""
    return HttpResponse(robots_body(is_indexable(request)), content_type="text/plain; charset=utf-8")


@require_GET
@cache_control(public=True, max_age=3600)
def sitemap_index(request: HttpRequest) -> HttpResponse:
    """GET /sitemap.xml -- every public URL in one list: the website's pages + published blog articles."""
    lines = []
    for loc, when in sitemap_urls():
        lastmod = f"<lastmod>{when:%Y-%m-%d}</lastmod>" if when else ""
        lines.append(f"  <url><loc>{escape(loc)}</loc>{lastmod}</url>")
    xml = f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="{SITEMAP_NS}">\n' + "\n".join(lines) + "\n</urlset>\n"
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
