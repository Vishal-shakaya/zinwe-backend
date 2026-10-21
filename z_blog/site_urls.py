from django.urls import re_path

from . import prerender

app_name = "z_blog_site"

# The website's own /blog URLs (outside /api/), for search engines: see z_blog/prerender.py.
# Routed here by nginx; locally the Vite dev server serves /blog, so these answer on :8000 only.
urlpatterns = [
    re_path(r"^blog/?$", prerender.blog_index, name="index"),
    re_path(r"^blog/series/?$", prerender.series_index, name="series-index"),
    re_path(r"^blog/series/(?P<slug>[-a-zA-Z0-9_]+)/?$", prerender.series_page, name="series"),
    re_path(r"^blog/series/(?P<slug>[-a-zA-Z0-9_]+)/(?P<episode>[-a-zA-Z0-9_]+)/?$", prerender.episode_page, name="episode"),
    re_path(r"^blog/(?P<slug>[-a-zA-Z0-9_]+)/?$", prerender.blog_article, name="article"),
]
