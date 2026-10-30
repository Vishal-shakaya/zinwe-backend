"""SEO round (30 Oct 2026): one title style, 140-160 character descriptions, sized images, complete article
structured data, breadcrumbs that match the visible trail, and sitemap dates that say when a page really changed."""
from __future__ import annotations

import io
import json
import re
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path

from django.core.files.base import ContentFile
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import prerender
from .models import Article, BlogImage
from .seo_meta import COVER_SIZE, clip, describe, headline, modified_at, page_title, sized_images
from .test_guides import GuideTestCase
from .test_series import SeriesTestCase
from .tests import STAFF

INDEX = """<!doctype html><html lang="en"><head><title>Zinwe</title>
<meta name="description" content="Default description." /></head><body><div id="root"></div></body></html>"""


def png(width: int, height: int) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(out, "PNG")
    return out.getvalue()


def ld_blocks(html: str) -> list[dict]:
    return [json.loads(m) for m in re.findall(r'<script type="application/ld\+json" data-prerender>(.*?)</script>', html, re.S)]


class TitleAndTextRulesTests(SimpleTestCase):
    def test_title_is_topic_then_brand_with_one_separator(self) -> None:
        self.assertEqual(page_title("Stop USB leaks"), "Stop USB leaks | Zinwe")
        self.assertEqual(page_title("Stop USB leaks | Zinwe"), "Stop USB leaks | Zinwe")  # typed brand not doubled
        self.assertEqual(page_title("Stop USB leaks — Zinwe"), "Stop USB leaks | Zinwe")
        self.assertEqual(page_title("Everyone Had Access", "The Last File"), "Everyone Had Access | The Last File | Zinwe")

    def test_long_title_drops_the_brand_instead_of_cutting_words(self) -> None:
        long = "Why files end up with the wrong person, and how to stop it"  # 58 characters
        self.assertEqual(page_title(long), long)
        ep = "The File That Almost Destroyed Everything"
        self.assertEqual(page_title(ep, "The Last File"), f"{ep} | The Last File")
        longer = "x" * 70
        self.assertEqual(page_title(longer, "Show"), longer)  # never truncated
        self.assertEqual(page_title("y" * 50, "A long series name"), "y" * 50 + " | Zinwe")

    def test_description_is_140_to_160_characters_at_a_boundary(self) -> None:
        short = "Short summary."
        self.assertEqual(describe(short), short)  # nothing more to say: left as it is
        body = "<p>" + " ".join(f"Sentence number {i} is here." for i in range(20)) + "</p>"
        d = describe(short, body)
        self.assertTrue(140 <= len(d) <= 160, (len(d), d))
        self.assertTrue(d.endswith("."))  # whole sentences when they fit
        words = describe("word " * 100)
        self.assertTrue(140 <= len(words) <= 160, len(words))
        self.assertTrue(words.endswith("word…"))
        self.assertEqual(clip("alpha beta gamma", 12), "alpha beta…")

    def test_headline_is_at_most_110_characters(self) -> None:
        self.assertLessEqual(len(headline("word " * 60)), 110)
        self.assertEqual(headline("A short title"), "A short title")


class SeoPagesTestCase(SeriesTestCase, GuideTestCase):
    def setUp(self) -> None:
        super().setUp()
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        (folder / "index.html").write_text(INDEX, encoding="utf-8")
        index = override_settings(ZINWE_FRONTEND_INDEX=str(folder / "index.html"))
        index.enable()
        self.addCleanup(index.disable)

    def image(self, width: int, height: int) -> BlogImage:
        img = BlogImage(content_type="image/png", size=1)
        img.file.save("upload.png", ContentFile(png(width, height)), save=False)
        img.save()
        return img

    def published(self, **extra) -> Article:  # type: ignore[no-untyped-def]
        a = self.create(**extra)
        self.client.post(f"{STAFF}{a['id']}/publish/", {}, format="json")
        return Article.objects.get(pk=a["id"])

    def page(self, path: str) -> str:
        self.anon()
        r = self.client.get(path)
        self.as_staff()
        self.assertEqual(r.status_code, 200, path)
        return r.content.decode()


class ArticlePageTests(SeoPagesTestCase):
    def test_title_description_and_cover(self) -> None:
        cover = self.image(1600, 840)
        inline = self.image(800, 450)
        a = self.published(
            body_html=f'<p>Intro.</p><p><img src="/api/blog/images/{inline.key}/" alt="A chart"></p>'
            '<p><img src="https://example.com/x.png" alt="Elsewhere"></p>'
        )
        Article.objects.filter(pk=a.pk).update(cover_image=cover, cover_alt="The USB policy screen")
        html = self.page(f"/blog/{a.slug}")
        self.assertIn("<title>How Zinwe stops USB data leaks | Zinwe</title>", html)
        # No meta description: built from the summary and the body, 140-160 characters.
        desc = re.search(r'<meta name="description" content="([^"]*)"', html)[1]
        self.assertTrue(desc.startswith("A 5-minute guide. Intro."), desc)
        # The page's own cover: real size, alt text, loads first.
        self.assertIn(
            f'<img src="/api/blog/images/{cover.key}/" alt="The USB policy screen" width="1600" height="840" '
            'fetchpriority="high" />', html,
        )
        # In-body images: our upload gets its size; every one loads lazily and decodes off the main thread.
        self.assertIn(
            f'<img src="/api/blog/images/{inline.key}/" alt="A chart" width="800" height="450" loading="lazy" '
            'decoding="async">', html,
        )
        self.assertIn('<img src="https://example.com/x.png" alt="Elsewhere" loading="lazy" decoding="async">', html)
        self.assertIn('<meta property="og:image:width" content="1600" data-prerender />', html)

    def test_unreadable_cover_is_assumed_1200_by_630(self) -> None:
        cover = BlogImage.objects.create(file="blog/missing.png", content_type="image/png", size=1)
        a = self.published()
        Article.objects.filter(pk=a.pk).update(cover_image=cover)
        html = self.page(f"/blog/{a.slug}")
        self.assertIn(f'width="{COVER_SIZE[0]}" height="{COVER_SIZE[1]}" fetchpriority="high"', html)
        self.assertIn('alt="How Zinwe stops USB data leaks"', html)  # no alt typed: the title

    def test_structured_data_is_complete(self) -> None:
        cover = self.image(1200, 630)
        a = self.published(title="A " + "very long headline " * 8, author_name="Asha Rao", tags="usb")
        Article.objects.filter(pk=a.pk).update(cover_image=cover)
        a.refresh_from_db()
        url = f"https://zinwe.test/blog/{a.slug}"
        blocks = {b["@type"]: b for b in ld_blocks(self.page(f"/blog/{a.slug}"))}
        post = blocks["BlogPosting"]
        self.assertLessEqual(len(post["headline"]), 110)
        self.assertTrue(140 <= len(post["description"]) <= 160 or len(post["description"]) < 140)
        self.assertEqual(post["image"], {"@type": "ImageObject", "url": f"https://zinwe.test{cover.url}", "width": 1200, "height": 630})
        self.assertEqual(post["author"], {"@type": "Person", "name": "Asha Rao"})
        self.assertEqual(post["publisher"]["logo"]["url"], "https://zinwe.test/brand/zinwe-logo-96.png")
        self.assertEqual(post["mainEntityOfPage"], {"@type": "WebPage", "@id": url})
        self.assertEqual(post["datePublished"], a.published_at.isoformat())
        self.assertIn("dateModified", post)
        # The visible trail and its BreadcrumbList agree.
        crumbs = [i["name"] for i in blocks["BreadcrumbList"]["itemListElement"]]
        self.assertEqual(crumbs, ["Zinwe", "Blog", a.title])

    def test_team_byline_is_the_company_and_no_cover_uses_the_share_image(self) -> None:
        a = self.published(author_name="Zinwe team")
        post = {b["@type"]: b for b in ld_blocks(self.page(f"/blog/{a.slug}"))}["BlogPosting"]
        self.assertEqual(post["author"]["@type"], "Organization")
        self.assertEqual(post["image"], "https://zinwe.test/brand/og-image.png")

    def test_scheduled_article_is_modified_no_earlier_than_published(self) -> None:
        a = self.published()
        later = timezone.now() + timedelta(minutes=5)
        Article.objects.filter(pk=a.pk).update(published_at=later)
        a.refresh_from_db()
        self.assertEqual(modified_at(a), later)

    def test_not_found_title(self) -> None:
        self.anon()
        html = self.client.get("/blog/nope").content.decode()
        self.assertIn("<title>Page not found | Zinwe</title>", html)
        self.assertNotIn("BreadcrumbList", html)  # a 404 has no structured data


class HubWordsTests(SimpleTestCase):
    def test_hub_titles_and_descriptions_follow_the_house_rules(self) -> None:
        for title in (prerender.LIST_TITLE, prerender.SERIES_TITLE, prerender.GUIDES_TITLE, prerender.NOT_FOUND_TITLE):
            self.assertTrue(title.endswith(" | Zinwe"), title)
            self.assertNotIn("—", title)
        for title in (prerender.LIST_TITLE, prerender.SERIES_TITLE, prerender.GUIDES_TITLE):
            self.assertTrue(40 <= len(title) <= 60, title)
        for desc in (prerender.LIST_DESCRIPTION, prerender.SERIES_DESCRIPTION, prerender.GUIDES_DESCRIPTION):
            self.assertTrue(140 <= len(desc) <= 160, desc)


class HubPageTests(SeoPagesTestCase):
    def test_blog_list_matches_the_website(self) -> None:
        self.published()
        html = self.page("/blog")
        self.assertIn(f"<title>{prerender.LIST_TITLE}</title>", html)
        self.assertIn(f"<h1>{prerender.LIST_H1}</h1>", html)
        self.assertEqual(html.count("<h1"), 1)
        self.assertIn('<meta name="description" content="Simple guides on protecting customer and company data', html)
        self.assertIn('<nav aria-label="Breadcrumb">', html)

    def test_series_pages_have_breadcrumbs_and_consistent_titles(self) -> None:
        show = self.make_series()
        ep = self.make_episode(show["id"], title="Everyone Had Access")
        hub = self.page("/blog/series")
        self.assertIn(f"<h1>{prerender.SERIES_H1}</h1>", hub)
        page = self.page("/blog/series/the-last-file")
        self.assertIn("<title>The Last File: a members-only series | Zinwe</title>", page)
        trail = {b["@type"]: b for b in ld_blocks(page)}["BreadcrumbList"]["itemListElement"]
        self.assertEqual([i["name"] for i in trail], ["Zinwe", "Blog", "Series", "The Last File"])
        self.assertEqual(trail[-1]["item"], "https://zinwe.test/blog/series/the-last-file")
        self.assertIn('<a href="/blog/series">Series</a> / <a href="/blog/series/the-last-file">The Last File</a>', page)
        episode = self.page(f"/blog/series/the-last-file/{ep['slug']}")
        self.assertIn("<title>Everyone Had Access | The Last File | Zinwe</title>", episode)
        blocks = {b["@type"]: b for b in ld_blocks(episode)}
        self.assertEqual(blocks["Article"]["publisher"]["logo"]["width"], 390)
        self.assertEqual(len(blocks["BreadcrumbList"]["itemListElement"]), 5)
        self.anon()
        self.assertIn("<title>Page not found | Zinwe</title>", self.client.get("/blog/series/nope").content.decode())

    def test_guide_pages(self) -> None:
        g = self.make_guide()
        self.assertIn(f"<h1>{prerender.GUIDES_H1}</h1>", self.page("/blog/guides"))
        html = self.page(f"/blog/guides/{g['slug']}")
        self.assertRegex(html, r"<title>[^<]+ \(free guide\)( \| Zinwe)?</title>")
        self.assertLessEqual(len(re.search(r"<title>([^<]+)</title>", html)[1]), 60)
        self.assertIn("BreadcrumbList", html)
        self.anon()
        self.assertIn("<title>Page not found | Zinwe</title>", self.client.get("/blog/guides/nope").content.decode())


class SizedImagesTests(SeoPagesTestCase):
    def test_existing_size_is_kept_and_markup_without_images_is_untouched(self) -> None:
        img = self.image(10, 20)
        html = f'<img src="/api/blog/images/{img.key}/" alt="" width="5" height="9">'
        self.assertEqual(sized_images(html), html[:-1] + ' loading="lazy" decoding="async">')
        self.assertEqual(sized_images("<p>No pictures.</p>"), "<p>No pictures.</p>")
        bad = '<img src="/api/blog/images/zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz/" alt="">'
        self.assertIn('loading="lazy"', sized_images(bad))


class SitemapDateTests(SeoPagesTestCase):
    def test_lastmod_is_the_publish_time_when_later_and_scheduled_is_not_listed(self) -> None:
        from .sitemap import entries

        live = self.published(title="Live now")
        went_live = timezone.now() - timedelta(hours=1)
        long_ago = went_live - timedelta(days=20)
        # Saved 20 days before it went live (scheduled): its date is the day it went live.
        Article.objects.filter(pk=live.pk).update(published_at=went_live, updated_at=long_ago)
        soon = self.published(title="Coming soon")
        Article.objects.filter(pk=soon.pk).update(published_at=timezone.now() + timedelta(days=1))
        found = dict(entries())
        self.assertEqual(found[f"/blog/{live.slug}"], went_live)
        self.assertEqual(found["/blog"], went_live)
        self.assertNotIn(f"/blog/{soon.slug}", found)
        self.assertFalse(any(p.endswith(soon.slug) for p in found))
