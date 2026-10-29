from django.urls import path

from . import views

app_name = "z_adminapi"

urlpatterns = [
    path("me/", views.MeView.as_view(), name="me"),
    path("metrics/", views.MetricsView.as_view(), name="metrics"),
    path("plans/", views.PlanListView.as_view(), name="plans"),
    path("pricing/", views.PricingView.as_view(), name="pricing"),
    path("pricing/<slug:code>/", views.PricingDetailView.as_view(), name="pricing-detail"),
    path("subscribers/", views.SubscriberListView.as_view(), name="subscribers"),
    path("subscribers/<int:pk>/", views.SubscriberDetailView.as_view(), name="subscriber-detail"),
    path("subscribers/<int:pk>/approve-payment/", views.ApprovePaymentView.as_view(), name="approve-payment"),
    path("subscribers/<int:pk>/reject-payment/", views.RejectPaymentView.as_view(), name="reject-payment"),
    path("subscribers/<int:pk>/block/", views.BlockView.as_view(), name="block"),
    path("subscribers/<int:pk>/unblock/", views.UnblockView.as_view(), name="unblock"),
    path("subscribers/<int:pk>/change-plan/", views.ChangePlanView.as_view(), name="change-plan"),
    path("subscribers/<int:pk>/set-password/", views.SetPasswordView.as_view(), name="set-password"),
    path(
        "subscribers/<int:pk>/send-password-reset/", views.SendPasswordResetView.as_view(), name="send-password-reset"
    ),
    path("badges/", views.BadgesView.as_view(), name="badges"),
    path("billing-settings/", views.BillingSettingsView.as_view(), name="billing-settings"),
    path("subscribers/<int:pk>/remark/", views.RemarkView.as_view(), name="remark"),
    path("subscribers/<int:pk>/emails/", views.CustomerEmailView.as_view(), name="emails"),
    path("payments/", views.PaymentListView.as_view(), name="payments"),
    path("payments/<int:pk>/approve/", views.PaymentApproveView.as_view(), name="payment-approve"),
    path("payments/<int:pk>/reject/", views.PaymentRejectView.as_view(), name="payment-reject"),
    path("payments/<int:pk>/invoice/pdf/", views.InvoicePdfView.as_view(), name="payment-invoice-pdf"),
    path("subscriptions/", views.SubscriptionListView.as_view(), name="subscriptions"),
    path("subscriptions/<int:pk>/", views.SubscriptionDetailView.as_view(), name="subscription-detail"),
    path("feedback/", views.FeedbackListView.as_view(), name="feedback"),
    path("releases/", views.ReleaseListView.as_view(), name="releases"),
    path("releases/reach/", views.ReleaseReachView.as_view(), name="release-reach"),
    path("releases/<int:pk>/", views.ReleaseDetailView.as_view(), name="release-detail"),
    path("announcements/", views.AnnouncementListView.as_view(), name="announcements"),
    path("announcements/reach/", views.AnnouncementReachView.as_view(), name="announcement-reach"),
    path("announcements/<int:pk>/", views.AnnouncementDetailView.as_view(), name="announcement-detail"),
    path("release-notes/", views.ReleaseNoteListView.as_view(), name="release-notes"),
    path("release-notes/<int:pk>/", views.ReleaseNoteDetailView.as_view(), name="release-note-detail"),
    path("logs/", views.LogsView.as_view(), name="logs"),
    path("logs/status/", views.LogsStatusView.as_view(), name="logs-status"),
    path("logs/ops/<int:pk>/resolve/", views.LogResolveView.as_view(), name="log-resolve"),
]
