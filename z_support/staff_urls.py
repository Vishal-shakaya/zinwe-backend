from django.urls import path

from . import views

app_name = "z_support_staff"

# Staff side, mounted at /api/admin/support/ (the admin app's Support tab).
urlpatterns = [
    path("", views.StaffTicketsView.as_view(), name="tickets"),
    path("<int:pk>/", views.StaffTicketView.as_view(), name="ticket"),
    path("<int:pk>/reply/", views.StaffReplyView.as_view(), name="reply"),
    path("<int:pk>/status/", views.StaffStatusView.as_view(), name="status"),
]
