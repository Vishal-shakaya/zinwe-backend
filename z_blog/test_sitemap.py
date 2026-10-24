"""FIX-010: /sitemap.xml (the sitemap robots.txt names) lists every public page: the website's pages, articles,
series, released episodes and guides -- and nothing unreleased, unpublished or private."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from django.test import override_settings

from z_main.seo import robots_body

from .test_guides import GuideTestCase
from .test_series import SeriesTestCase
from .tests import STAFF

PAGES_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    "  <url><loc>https://zinwe.io/</loc><lastmod>2026-10-01</lastmod></url>\n"
    "  <url><loc>https://zinwe.io/plans</loc><lastmod>2026-10-01</lastmod></url>\n"
    "  <url><loc>https://zinwe.io/blog</loc><lastmod>2026-10-01</lastmod></url>\n"
    "  <url><loc>https://zinwe.io/blog/series</loc><lastmod>2026-10-01</lastmod></url>\n</urlset>\n"
)


class RootSitemapTests(SeriesTestCase, GuideTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.dist = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dist, True)
        (self.dist / "index.html").write_text("<html></html>", encoding="utf-8")
        (self.dist / "sitemap-pages.xml").write_text(PAGES_XML, encoding="utf-8")

    def sitemap(self) -> str:
        self.anon()
        with override_settings(ZINWE_APP_URL="https://zinwe.io", ZINWE_FRONTEND_INDEX=str(self.dist / "index.html")):
            body = self.client.get("/sitemap.xml").content.decode()
        self.as_staff()
        return body

    def test_series_episodes_and_guides_are_in_the_root_sitemap(self) -> None:
        show = self.make_series()
        live = self.make_episode(show["id"])
        early = self.make_episode(show["id"], publish=False, title="Not out yet")
        guide = self.make_guide()
        draft = self.make_guide(publish=False, title="A draft guide")
        self.make_series(title="Hidden show", status="draft")
        article = self.create(title="Stop invoice scams")
        self.client.post(f"{STAFF}{article['id']}/publish/", {}, format="json")
        body = self.sitemap()
        for path in ("/", "/plans", "/blog", "/blog/series", "/blog/series/the-last-file",
                     f"/blog/series/the-last-file/{live['slug']}", "/blog/guides", f"/blog/guides/{guide['slug']}",
                     f"/blog/{article['slug']}"):
            self.assertEqual(body.count(f"<loc>https://zinwe.io{path}</loc>"), 1, path)
        for missing in (early["slug"], draft["slug"], "hidden-show", "/api/"):
            self.assertNotIn(missing, body)
        self.assertNotIn("<sitemapindex", body)

    def test_robots_names_the_complete_sitemap(self) -> None:
        with override_settings(ZINWE_APP_URL="https://zinwe.io"):
            robots = robots_body(True)
        self.assertEqual([l for l in robots.splitlines() if l.startswith("Sitemap:")], ["Sitemap: https://zinwe.io/sitemap.xml"])
