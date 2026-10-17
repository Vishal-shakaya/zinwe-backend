from django.urls import path

from z_support.views import ContactView

from . import views

app_name = "z_main"

urlpatterns = [
    path("health/", views.HealthView.as_view(), name="health"),
    path("contact/", ContactView.as_view(), name="contact"),  # public website contact form
]
