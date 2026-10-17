"""robots.txt and the sitemaps at the site root (z_main/seo.py)."""
from __future__ import annotations

import tempfile
from pathlib import Path

from django.test import TestCase, override_settings

PAGES_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    "  <url><loc>https://zinwe.io/</loc></url>\n  <url><loc>https://zinwe.io/plans</loc></url>\n</urlset>\n"
)

PROD = {"ZINWE_SEARCH_INDEXING": True, "ZINWE_APP_URL": "https://zinwe.io", "ALLOWED_HOSTS": ["*"]}


class SeoFilesTests(TestCase):
    def setUp(self) -> None:
        self.dist = Path(tempfile.mkdtemp())
        (self.dist / "index.html").write_text("<html></html>", encoding="utf-8")
        (self.dist / "sitemap-pages.xml").write_text(PAGES_XML, encoding="utf-8")
        self.index = str(self.dist / "index.html")

    # --- robots.txt -------------------------------------------------------------

    @override_settings(**PROD)
    def test_production_site_is_open_except_private_areas(self) -> None:
        response = self.client.get("/robots.txt", HTTP_HOST="zinwe.io")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/plain; charset=utf-8")
        body = response.content.decode()
        self.assertNotIn("Disallow: /\n", body)
        for line in ("Disallow: /app", "Disallow: /onboarding", "Disallow: /quote/", "Disallow: /api/", "Allow: /api/blog/"):
            self.assertIn(line, body)
        self.assertNotIn("Disallow: /login", body)  # noindex pages stay fetchable so the tag is seen
        self.assertIn("Sitemap: https://zinwe.io/sitemap.xml", body)

    @override_settings(**PROD)
    def test_admin_and_api_hosts_are_closed_even_in_production(self) -> None:
        for host in ("prod.admin.zinwe.io", "prod.backend.zinwe.io"):
            body = self.client.get("/robots.txt", HTTP_HOST=host).content.decode()
            self.assertIn("Disallow: /\n", body, host)
            self.assertNotIn("Sitemap:", body)

    @override_settings(ZINWE_SEARCH_INDEXING=False, ZINWE_APP_URL="https://dev.frontend.zinwe.io", ALLOWED_HOSTS=["*"])
    def test_dev_is_closed(self) -> None:
        body = self.client.get("/robots.txt", HTTP_HOST="dev.frontend.zinwe.io").content.decode()
        self.assertIn("User-agent: *\nDisallow: /\n", body)

    # --- sitemaps ----------------------------------------------------------------

    def test_sitemap_index_lists_pages_and_the_blog_once_it_has_an_article(self) -> None:
        with override_settings(**PROD, ZINWE_FRONTEND_INDEX=self.index):
            body = self.client.get("/sitemap.xml").content.decode()
            self.assertIn("<sitemapindex", body)
            self.assertIn("<loc>https://zinwe.io/sitemap-pages.xml</loc>", body)
            self.assertNotIn("/api/blog/sitemap.xml", body)  # no published article yet

            self._publish_article()
            body = self.client.get("/sitemap.xml").content.decode()
            self.assertIn("<loc>https://zinwe.io/api/blog/sitemap.xml</loc>", body)

    def test_pages_sitemap_moves_to_this_environments_address(self) -> None:
        with override_settings(ZINWE_APP_URL="https://dev.frontend.zinwe.io", ZINWE_FRONTEND_INDEX=self.index):
            response = self.client.get("/sitemap-pages.xml")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/xml")
        body = response.content.decode()
        self.assertIn("<loc>https://dev.frontend.zinwe.io/plans</loc>", body)
        self.assertNotIn("https://zinwe.io/", body)

    def test_missing_build_is_a_404_and_the_index_still_answers(self) -> None:
        with override_settings(**PROD, ZINWE_FRONTEND_INDEX=str(self.dist / "nowhere" / "index.html")):
            self.assertEqual(self.client.get("/sitemap-pages.xml").status_code, 404)
            response = self.client.get("/sitemap.xml")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("sitemap-pages.xml", response.content.decode())

    def _publish_article(self) -> None:
        from django.utils import timezone

        from z_blog.models import Article

        Article.objects.create(
            title="How to stop USB data leaks", slug="stop-usb-leaks", summary="A short guide.",
            body_html="<p>Text</p>", status=Article.Status.PUBLISHED, published_at=timezone.now(),
        )
