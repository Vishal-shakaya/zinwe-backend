"""Root URL configuration. Every API lives under the `api/` prefix."""
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("z_main.urls")),
    path("api/auth/", include("z_auth.urls")),
    path("api/analytics/", include("z_analytics.urls")),
    path("api/devices/", include("z_devices.urls")),
    path("api/support/", include("z_support.urls")),
    path("api/admin/support/", include("z_support.staff_urls")),
    path("api/blog/", include("z_blog.urls")),
    path("api/admin/blog/", include("z_blog.staff_urls")),
    path("", include("z_blog.site_urls")),  # /blog pre-rendered for search engines
    path("", include("z_main.site_urls")),  # /robots.txt, /sitemap.xml, /sitemap-pages.xml
    path("api/admin/", include("z_adminapi.urls")),
]

admin.site.site_header = "Zinwe admin"
admin.site.site_title = "Zinwe admin"
admin.site.index_title = "Back office"
