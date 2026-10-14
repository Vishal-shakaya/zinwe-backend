from django.urls import path

from . import views

app_name = "z_support"

# Owner side, mounted at /api/support/.
urlpatterns = [
    path("tickets/", views.OwnerTicketsView.as_view(), name="tickets"),
    path("tickets/<int:pk>/", views.OwnerTicketView.as_view(), name="ticket"),
    path("tickets/<int:pk>/messages/", views.OwnerReplyView.as_view(), name="ticket-reply"),
]
