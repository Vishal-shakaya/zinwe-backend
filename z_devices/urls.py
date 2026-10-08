from django.urls import path

from . import views

app_name = "z_devices"

urlpatterns = [
    path("", views.DeviceListView.as_view(), name="list"),
    path("enroll/", views.EnrollView.as_view(), name="enroll"),
    path("heartbeat/", views.HeartbeatView.as_view(), name="heartbeat"),
    path("enrollment-key/", views.EnrollmentKeyView.as_view(), name="enrollment-key"),
    path(
        "enrollment-key/rotate/",
        views.EnrollmentKeyRotateView.as_view(),
        name="enrollment-key-rotate",
    ),
    path("policy/", views.PolicyView.as_view(), name="policy"),
    path("policy/current/", views.CurrentPolicyView.as_view(), name="policy-current"),
    path("events/", views.EventFeedView.as_view(), name="events"),
    path("events/upload/", views.EventUploadView.as_view(), name="events-upload"),
]
