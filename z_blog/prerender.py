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

# The website build (zinwe-frontend build/seoPages.ts) pre-renders its home page into this same
# index.html: a static copy of the page beside #root, plus a canonical link and structured data marked
# `data-prerender`. They describe the home page, so a blog page drops them and adds its own.
_HOME_ONLY = (
    re.compile(r"<!--seo-static-->.*?<!--/seo-static-->", re.S),
    re.compile(r"<link[^>]*\bdata-prerender\b[^>]*>"),
    re.compile(r'<script type="application/ld\+json" data-prerender>.*?</script>', re.S),
)


def _without_home_page(html: str) -> str:
    for pattern in _HOME_ONLY:
        html = pattern.sub("", html)
    return html


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
        self.html = _without_home_page(_index_html())
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
    page.meta("name", "twitter:title", LIST_TITLE)
    page.meta("name", "twitter:description", LIST_DESCRIPTION)
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
    page.meta("name", "twitter:title", title)
    page.meta("name", "twitter:description", description)
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


# --- Members-only series (/blog/series...) -------------------------------------------------
# Search engines get each episode's public opening only (the same thing a visitor sees before joining),
# marked as members-only content in the structured data, so nothing is cloaked.

SERIES_TITLE = "Zinwe series — members-only stories for business owners"
SERIES_DESCRIPTION = (
    "Short business-drama series about the small mistakes that put company data at risk, each episode with a "
    "10-minute fix. Free membership."
)


def _series_nav(show: Any = None) -> str:
    crumbs = '<a href="/">Zinwe</a> / <a href="/blog">Blog</a> / <a href="/blog/series">Series</a>'
    if show is not None:
        crumbs += f' / <a href="/blog/series/{_attr(show.slug)}">{escape(show.title)}</a>'
    return f"<nav>{crumbs}</nav>"


def _describe(page: _Page, title: str, description: str, url: str, image: str = "") -> None:
    page.meta("name", "description", description)
    page.meta("property", "og:title", title)
    page.meta("property", "og:description", description)
    page.meta("name", "twitter:title", title)
    page.meta("name", "twitter:description", description)
    page.meta("property", "og:url", url)
    if image:
        page.meta("property", "og:image", image)
        page.meta("name", "twitter:image", image)
    page.link("canonical", url)


def _not_found(what: str) -> HttpResponse:
    page = _Page()
    page.title(f"{what.capitalize()} not found — Zinwe")
    page.meta("name", "robots", "noindex")
    return page.render(
        f'{_series_nav()}<h1>This {what} isn\'t here</h1><p><a href="/blog/series">All series</a></p>', status=404
    )


def series_index(request: HttpRequest) -> HttpResponse:
    """GET /blog/series -- every live series."""
    from . import series as svc

    page = _Page()
    page.title(SERIES_TITLE)
    _describe(page, SERIES_TITLE, SERIES_DESCRIPTION, f"{_site()}/blog/series")
    items = "".join(
        f'<li><a href="/blog/series/{_attr(s.slug)}">{escape(s.title)}</a>'
        f"{f'<p>{escape(s.tagline)}</p>' if s.tagline else ''}</li>"
        for s in svc.live_series()
    )
    return page.render(f"{_series_nav()}<h1>Zinwe series</h1><p>{escape(SERIES_DESCRIPTION)}</p><ul>{items}</ul>")


def series_page(request: HttpRequest, slug: str) -> HttpResponse:
    """GET /blog/series/<slug> -- a series and its released episodes."""
    from . import series as svc

    show = svc.live_series().filter(slug=slug).first()
    if show is None:
        return _not_found("series")
    url = f"{_site()}/blog/series/{show.slug}"
    image = f"{_site()}{show.cover_image.url}" if show.cover_image else ""
    page = _Page()
    page.title(f"{show.title} — a Zinwe series")
    _describe(page, show.title, show.tagline or show.description[:160], url, image)
    episodes = svc.lineup(show).released
    page.json_ld({
        "@context": "https://schema.org",
        "@type": "CreativeWorkSeries",
        "name": show.title,
        "description": show.description or show.tagline,
        "url": url,
        **({"image": image} if image else {}),
        "publisher": {"@type": "Organization", "name": "Zinwe"},
        "hasPart": [
            {"@type": "Article", "headline": a.title, "url": f"{_site()}{svc.path(a)}", "position": a.episode}
            for a in episodes
        ],
    })
    items = "".join(
        f'<li><a href="{_attr(svc.path(a))}">{escape(svc.label(a))}: {escape(a.title)}</a>'
        f"{f'<p>{escape(a.summary)}</p>' if a.summary else ''}</li>"
        for a in episodes
    )
    body = (
        f"{_series_nav()}<h1>{escape(show.title)}</h1>"
        f"{f'<p>{escape(show.tagline)}</p>' if show.tagline else ''}"
        f"{f'<p>{escape(show.description)}</p>' if show.description else ''}<ol>{items}</ol>"
    )
    return page.render(body)


def episode_page(request: HttpRequest, slug: str, episode: str) -> HttpResponse:
    """GET /blog/series/<slug>/<episode> -- the episode's public opening (the full text is for members)."""
    from . import series as svc

    a = svc.released_episode(slug, episode)
    if a is None:
        return _not_found("episode")
    show = a.series
    url = f"{_site()}{svc.path(a)}"
    title = a.seo_title or a.title
    cover = a.cover_image or show.cover_image
    image = f"{_site()}{cover.url}" if cover else ""
    page = _Page()
    page.title(f"{title} — {show.title}")
    _describe(page, title, a.meta_description or a.summary, url, image)
    page.meta("property", "og:type", "article")
    page.meta("property", "article:published_time", a.published_at.isoformat())
    free = a.free_preview
    page.json_ld({
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": title,
        "description": a.meta_description or a.summary,
        "datePublished": a.published_at.isoformat(),
        "dateModified": a.updated_at.isoformat(),
        "author": {"@type": "Organization", "name": a.author_name or "Zinwe"},
        "publisher": {
            "@type": "Organization", "name": "Zinwe",
            "logo": {"@type": "ImageObject", "url": f"{_site()}/brand/zinwe-logo-96.png"},
        },
        "mainEntityOfPage": url,
        "isPartOf": {"@type": "CreativeWorkSeries", "name": show.title, "url": f"{_site()}/blog/series/{show.slug}"},
        **({"image": image} if image else {}),
        "isAccessibleForFree": free,
        **({} if free else {"hasPart": {"@type": "WebPageElement", "isAccessibleForFree": False, "cssSelector": ".ep-members"}}),
    })
    img = f'<img src="{_attr(cover.url)}" alt="{_attr(a.cover_alt or show.cover_alt)}" />' if cover else ""
    members = "" if free else "<p>The rest of this episode is for members. Membership is free.</p>"
    # teaser_html is allow-list cleaned on save (services.clean_body), the same HTML the app shows.
    body = (
        f"{_series_nav(show)}<article><p>{escape(svc.label(a))}</p><h1>{escape(a.title)}</h1>"
        f"{f'<p>{escape(a.summary)}</p>' if a.summary else ''}{img}{a.teaser_html}"
        f'<div class="ep-members">{members}</div></article>'
    )
    return page.render(body)


# --- Free guides (/blog/guides...) ---------------------------------------------------------
# The guide's page is public (cover, contents, sample pages); the PDF itself is a members' download.

GUIDES_TITLE = "Free guides for small businesses — Zinwe"
GUIDES_DESCRIPTION = (
    "Plain-English e-guides with workbooks and 30-day plans: protect your business online, keep data safe, stop scams "
    "and communicate better at work. Free for members."
)


def _guides_nav() -> str:
    return '<nav><a href="/">Zinwe</a> / <a href="/blog">Blog</a> / <a href="/blog/guides">Guides</a></nav>'


def guides_index(request: HttpRequest) -> HttpResponse:
    """GET /blog/guides -- every live guide."""
    from . import guides

    page = _Page()
    page.title(GUIDES_TITLE)
    _describe(page, GUIDES_TITLE, GUIDES_DESCRIPTION, f"{_site()}/blog/guides")
    books = list(guides.live())
    items = "".join(
        f'<li><a href="{_attr(guides.path(g))}">{escape(g.title)}</a>'
        f"{f'<p>{escape(g.summary)}</p>' if g.summary else ''}</li>"
        for g in books
    )
    return page.render(f"{_guides_nav()}<h1>Free guides for small businesses</h1><p>{escape(GUIDES_DESCRIPTION)}</p><ul>{items}</ul>")


def guide_page(request: HttpRequest, slug: str) -> HttpResponse:
    """GET /blog/guides/<slug> -- a guide's page: what's inside, who it's for."""
    from . import guides

    g = guides.live().filter(slug=slug).first()
    if g is None:
        page = _Page()
        page.title("Guide not found — Zinwe")
        page.meta("name", "robots", "noindex")
        return page.render(f'{_guides_nav()}<h1>This guide isn\'t here</h1><p><a href="/blog/guides">All guides</a></p>', status=404)
    url = f"{_site()}{guides.path(g)}"
    image = f"{_site()}{g.cover_image.url}" if g.cover_image else ""
    description = g.summary or g.subtitle
    page = _Page()
    page.title(f"{g.title} (free guide) — Zinwe")
    _describe(page, g.title, description, url, image)
    page.json_ld({
        "@context": "https://schema.org",
        "@type": "Book",
        "name": g.title,
        "description": description,
        "bookFormat": "https://schema.org/EBook",
        "numberOfPages": g.page_count or None,
        "inLanguage": "en",
        "isAccessibleForFree": True,
        "url": url,
        **({"image": image} if image else {}),
        "author": {"@type": "Organization", "name": "Zinwe"},
        "publisher": {"@type": "Organization", "name": "Zinwe"},
    })
    chapters = "".join(_chapter(c) for c in g.contents or [])
    paras = "".join(f"<p>{escape(p.strip())}</p>" for p in (g.description or "").splitlines() if p.strip())
    cover = ""
    if g.cover_image:
        cover = f'<img src="{_attr(g.cover_image.url)}" alt="{_attr(g.cover_alt or g.title)}" />'
    subtitle = f"<p>{escape(g.subtitle)}</p>" if g.subtitle else ""
    inside = f"<h2>What is inside</h2><ol>{chapters}</ol>" if chapters else ""
    body = (
        f"{_guides_nav()}<article><p>Free guide</p><h1>{escape(g.title)}</h1>{subtitle}{cover}{paras}{inside}"
        "<p>Free for members: enter your email on this page to download the PDF.</p></article>"
    )
    return page.render(body)


def _chapter(c: dict) -> str:
    note = c.get("note") or ""
    return f"<li>{escape(c.get('title') or '')}{' — ' + escape(note) if note else ''}</li>"
