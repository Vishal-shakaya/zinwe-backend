"""
Search-engine pre-rendering for the website's blog pages (/blog and /blog/<slug>).

The website is a single-page app: its index.html has an empty <div id="root"> and generic meta
tags, and React fills in the rest. Crawlers and link previews that don't run JavaScript would see
only that. So nginx sends /blog* here, and we return the *same* built index.html with:

  - the page's <title>, description and Open Graph / Twitter tags set,
  - canonical, article:* and schema.org JSON-LD tags added,
  - the article (or the article list) as plain HTML inside #root.

Changed tags carry `data-prerender-content` (the original value) and added ones `data-prerender`;
the app restores / removes them on start (zinwe-frontend src/lib/prerender.ts) and then renders the
page as usual, replacing #root. Nothing here is needed for the app to work; it's for robots.

Server: `location ~ ^/blog(/|$) { proxy_pass http://127.0.0.1:<API_PORT>; }` in the site's nginx
config, and ZINWE_FRONTEND_INDEX pointing at the deployed dist/index.html.
"""
from __future__ import annotations

import json
import logging
import re
from html import escape
from pathlib import Path
from typing import Any

from django.conf import settings
from django.http import HttpRequest, HttpResponse

from . import services
from .models import Article

logger = logging.getLogger("zinwe.blog.prerender")

LIST_TITLE = "Blog — Zinwe"
LIST_DESCRIPTION = "Guides, product releases, press and insights on protecting small-business data, from the team behind Zinwe."
LIST_LIMIT = 30

# Readable before the app's CSS and JS arrive (and for text-only crawlers); the app replaces it.
STYLE = (
    "<style data-prerender>.prerender{max-width:760px;margin:40px auto;padding:0 16px;"
    "font-family:Geist,system-ui,-apple-system,Segoe UI,sans-serif;line-height:1.7;color:#15161a}"
    ".prerender img{max-width:100%;height:auto}.prerender a{color:#c2410c}"
    ".prerender nav{margin-bottom:24px;font-size:14px}</style>"
)

FALLBACK_INDEX = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8" />'
    '<meta name="viewport" content="width=device-width, initial-scale=1" /><title>Zinwe</title>'
    '<meta name="description" content="" /></head><body><div id="root"></div></body></html>'
)

_cache: dict[str, Any] = {"path": None, "mtime": None, "html": None}


def _index_html() -> str:
    """The built website's index.html (re-read when it changes), or a bare shell if it isn't built."""
    path = Path(getattr(settings, "ZINWE_FRONTEND_INDEX", "") or "")
    try:
        mtime = path.stat().st_mtime
    except OSError:
        if _cache["path"] != "missing":
            logger.warning("Website index.html not found at %r; blog pages are served as plain HTML.", str(path))
            _cache.update(path="missing", mtime=None, html=None)
        return FALLBACK_INDEX
    if _cache["path"] != str(path) or _cache["mtime"] != mtime:
        _cache.update(path=str(path), mtime=mtime, html=path.read_text(encoding="utf-8"))
    return _cache["html"]


def _attr(value: str) -> str:
    return escape(value or "", quote=True)


class _Page:
    """Builds one pre-rendered page from the index.html template."""

    def __init__(self) -> None:
        self.html = _index_html()
        self.extra: list[str] = [STYLE]

    def title(self, text: str) -> None:
        tag = f"<title>{escape(text)}</title>"
        self.html, n = re.subn(r"<title>.*?</title>", lambda _: tag, self.html, count=1, flags=re.S)
        if not n:
            self.extra.append(tag.replace("<title>", "<title data-prerender>"))

    def meta(self, attr: str, key: str, content: str) -> None:
        """Set an existing <meta> (remembering its value for the app to restore) or add one."""
        pattern = re.compile(rf'<meta\s+{attr}="{re.escape(key)}"\s+content="([^"]*)"')
        new = f'<meta {attr}="{key}" content="{_attr(content)}"'
        # m[1] is the original value as written in index.html (already HTML-escaped).
        self.html, n = pattern.subn(lambda m: f'{new} data-prerender-content="{m[1]}"', self.html, count=1)
        if not n:
            self.extra.append(f'<meta {attr}="{key}" content="{_attr(content)}" data-prerender />')

    def link(self, rel: str, href: str) -> None:
        self.extra.append(f'<link rel="{rel}" href="{_attr(href)}" data-prerender />')

    def json_ld(self, data: dict[str, Any]) -> None:
        # No "<" at all inside the <script>: nothing in it can close or confuse the tag.
        payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
        self.extra.append(f'<script type="application/ld+json" data-prerender>{payload}</script>')

    def render(self, body: str, status: int = 200) -> HttpResponse:
        html = self.html.replace("</head>", "".join(self.extra) + "</head>", 1)
        html = re.sub(
            r'<div id="root">\s*</div>',
            lambda _: f'<div id="root"><div class="prerender">{body}</div></div>',
            html,
            count=1,
        )
        response = HttpResponse(html, status=status, content_type="text/html; charset=utf-8")
        response["Cache-Control"] = "public, max-age=300"
        return response


def _site() -> str:
    return settings.ZINWE_APP_URL


def _nav() -> str:
    return '<nav><a href="/">Zinwe</a> / <a href="/blog">Blog</a></nav>'


def _date(article: Article) -> str:
    return f'<time datetime="{article.published_at.isoformat()}">{article.published_at:%d %b %Y}</time>'


def blog_index(request: HttpRequest) -> HttpResponse:
    """GET /blog -- the latest articles as a crawlable list."""
    url = f"{_site()}/blog"
    page = _Page()
    page.title(LIST_TITLE)
    page.meta("name", "description", LIST_DESCRIPTION)
    page.meta("property", "og:title", LIST_TITLE)
    page.meta("property", "og:description", LIST_DESCRIPTION)
    page.meta("property", "og:url", url)
    page.link("canonical", url)
    page.link("alternate", f"{_site()}/api/blog/rss.xml")
    articles = list(services.published()[:LIST_LIMIT])
    page.json_ld({
        "@context": "https://schema.org",
        "@type": "Blog",
        "name": "Zinwe blog",
        "url": url,
        "description": LIST_DESCRIPTION,
        "blogPost": [
            {"@type": "BlogPosting", "headline": a.title, "url": f"{url}/{a.slug}", "datePublished": a.published_at.isoformat()}
            for a in articles
        ],
    })
    items = "".join(
        f'<li><a href="/blog/{_attr(a.slug)}">{escape(a.title)}</a> '
        f"<small>{escape(a.get_type_display())} · {_date(a)}</small>"
        f"{f'<p>{escape(a.summary)}</p>' if a.summary else ''}</li>"
        for a in articles
    )
    body = (
        f"{_nav()}<h1>Guides, releases and news</h1>"
        "<p>How small teams keep their data safe, what's new in Zinwe, and what we're learning along the way.</p>"
        f"<ul>{items}</ul>"
    )
    return page.render(body)


def blog_article(request: HttpRequest, slug: str) -> HttpResponse:
    """GET /blog/<slug> -- one published article, fully readable without JavaScript."""
    article = services.published().filter(slug=slug).first()
    page = _Page()
    if article is None:
        page.title("Article not found — Zinwe")
        page.meta("name", "robots", "noindex")
        return page.render(f'{_nav()}<h1>This article isn\'t here</h1><p><a href="/blog">All articles</a></p>', status=404)

    url = f"{_site()}/blog/{article.slug}"
    title = article.seo_title or article.title
    description = article.meta_description or article.summary
    image = f"{_site()}{article.cover_image.url}" if article.cover_image else ""
    page.title(f"{title} — Zinwe")
    page.meta("name", "description", description)
    page.meta("property", "og:type", "article")
    page.meta("property", "og:title", title)
    page.meta("property", "og:description", description)
    page.meta("property", "og:url", url)
    page.meta("property", "article:published_time", article.published_at.isoformat())
    page.meta("property", "article:modified_time", article.updated_at.isoformat())
    page.meta("property", "article:section", article.get_type_display())
    if image:
        page.meta("property", "og:image", image)
        page.meta("property", "og:image:alt", article.cover_alt or article.title)
        page.meta("name", "twitter:image", image)
    page.link("canonical", url)
    page.json_ld({
        "@context": "https://schema.org",
        "@type": "NewsArticle" if article.type == Article.Type.PRESS_RELEASE else "BlogPosting",
        "headline": title,
        "description": description,
        "datePublished": article.published_at.isoformat(),
        "dateModified": article.updated_at.isoformat(),
        "author": {"@type": "Organization", "name": article.author_name or "Zinwe"},
        "publisher": {
            "@type": "Organization", "name": "Zinwe",
            "logo": {"@type": "ImageObject", "url": f"{_site()}/brand/zinwe-logo-96.png"},
        },
        "mainEntityOfPage": url,
        **({"image": image} if image else {}),
        "keywords": ", ".join(article.tag_list),
    })
    cover = f'<img src="{_attr(article.cover_image.url)}" alt="{_attr(article.cover_alt)}" />' if image else ""
    byline = " · ".join(
        x for x in (escape(article.author_name), _date(article), f"{article.reading_minutes} min read") if x
    )
    # body_html is allow-list cleaned on save (services.clean_body), the same HTML the app shows.
    body = (
        f"{_nav()}<article><p>{escape(article.get_type_display())}</p><h1>{escape(article.title)}</h1>"
        f"{f'<p>{escape(article.summary)}</p>' if article.summary else ''}<p><small>{byline}</small></p>"
        f"{cover}{article.body_html}</article>"
    )
    return page.render(body)
