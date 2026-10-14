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
    path("members/", views.MemberListCreateView.as_view(), name="members"),
    path(
        "members/<int:member_id>/rotate-id/",
        views.MemberRotateIdView.as_view(),
        name="member-rotate-id",
    ),
    path(
        "members/<int:member_id>/deactivate/",
        views.MemberDeactivateView.as_view(),
        name="member-deactivate",
    ),
    path(
        "members/<int:member_id>/message/",
        views.MemberMessageView.as_view(),
        name="member-message",
    ),
    path(
        "members/<int:member_id>/messages/",
        views.MemberThreadView.as_view(),
        name="member-thread",
    ),
    path("messages/", views.DeviceMessagesView.as_view(), name="messages"),
    path("messages/read/", views.DeviceMessagesReadView.as_view(), name="messages-read"),
    path("releases/<int:pk>/download/", views.DeviceReleaseDownloadView.as_view(), name="release-download"),
    path("policy/", views.PolicyView.as_view(), name="policy"),
    path("policy/current/", views.CurrentPolicyView.as_view(), name="policy-current"),
    path("events/", views.EventFeedView.as_view(), name="events"),
    path("events/upload/", views.EventUploadView.as_view(), name="events-upload"),
    path("events/<int:event_id>/email/", views.EventEmailView.as_view(), name="event-email"),
    path("work-log/", views.WorkLogView.as_view(), name="work-log"),
    path("work-log/<int:log_id>/decision/", views.BreakDecisionView.as_view(), name="break-decision"),
    path("metrics/", views.MetricsFeedView.as_view(), name="metrics"),
    path("metrics/top/", views.MetricsTopView.as_view(), name="metrics-top"),
    path("metrics/upload/", views.MetricUploadView.as_view(), name="metrics-upload"),
]
