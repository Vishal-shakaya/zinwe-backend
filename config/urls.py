"""Root URL configuration. Every API lives under the `api/` prefix."""
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("z_main.urls")),
    path("api/auth/", include("z_auth.urls")),
    path("api/analytics/", include("z_analytics.urls")),
    path("api/devices/", include("z_devices.urls")),
]

admin.site.site_header = "Zinwe admin"
admin.site.site_title = "Zinwe admin"
admin.site.index_title = "Back office"
