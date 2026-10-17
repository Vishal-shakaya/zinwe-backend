from django.urls import path

from . import seo

app_name = "z_main_site"

# Site-root files for search engines (outside /api/): see z_main/seo.py. Routed here by nginx; locally the
# Vite dev server proxies them to :8000.
urlpatterns = [
    path("robots.txt", seo.robots_txt, name="robots"),
    path("sitemap.xml", seo.sitemap_index, name="sitemap"),
    path("sitemap-pages.xml", seo.sitemap_pages, name="sitemap-pages"),
]
