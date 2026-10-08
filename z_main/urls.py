from django.urls import path

from . import views

app_name = "z_main"

urlpatterns = [
    path("health/", views.HealthView.as_view(), name="health"),
]
