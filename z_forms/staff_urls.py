from django.urls import path

from . import views

app_name = "z_forms_staff"

# Staff side, mounted at /api/admin/forms/ (the admin app's Forms tab).
urlpatterns = [
    path("", views.StaffFormsView.as_view(), name="forms"),
    path("<int:pk>/", views.StaffFormView.as_view(), name="form"),
]
