from django.urls import path

from . import views

app_name = "z_analytics"

urlpatterns = [
    path("summary/", views.SummaryView.as_view(), name="summary"),
]
