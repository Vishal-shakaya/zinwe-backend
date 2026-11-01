from django.urls import path
from django.views.decorators.csrf import csrf_exempt

from . import views

app_name = "z_marketing"

urlpatterns = [
    path("unsubscribe/<str:token>/", views.unsubscribe_view, name="unsubscribe"),
    path("sheet-import/", csrf_exempt(views.SheetImportView.as_view()), name="sheet-import"),
]
