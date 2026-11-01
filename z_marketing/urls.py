from django.urls import path

from . import views

app_name = "z_marketing"

urlpatterns = [
    path("unsubscribe/<str:token>/", views.unsubscribe_view, name="unsubscribe"),
]
