from django.urls import path

from . import views

app_name = "z_marketing_staff"

urlpatterns = [
    path("audiences/", views.AudienceListView.as_view(), name="audiences"),
    path("audiences/<int:pk>/", views.AudienceDetailView.as_view(), name="audience"),
    path("campaigns/", views.CampaignListView.as_view(), name="campaigns"),
    path("campaigns/<int:pk>/", views.CampaignDetailView.as_view(), name="campaign"),
    path("campaigns/<int:pk>/emails/<int:n>/", views.EmailStepView.as_view(), name="email"),
    path("campaigns/<int:pk>/contacts/", views.ContactListView.as_view(), name="contacts"),
    path("campaigns/<int:pk>/contacts/import/", views.ContactImportView.as_view(), name="contacts-import"),
    path("campaigns/<int:pk>/<slug:action>/", views.CampaignActionView.as_view(), name="campaign-action"),
    path("contacts/<int:pk>/act/", views.ContactActView.as_view(), name="contact-act"),
    path("blocked/", views.BlockedView.as_view(), name="blocked"),
]
