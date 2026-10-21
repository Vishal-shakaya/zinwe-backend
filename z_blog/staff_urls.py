from django.urls import path

from . import newsletter_views, series_views, views

app_name = "z_blog_staff"

# Staff side, mounted at /api/admin/blog/ (the admin app's Blog tab).
urlpatterns = [
    path("articles/", views.StaffArticlesView.as_view(), name="articles"),
    path("articles/<int:pk>/", views.StaffArticleView.as_view(), name="article"),
    path("articles/<int:pk>/publish/", views.StaffPublishView.as_view(), name="publish"),
    path("articles/<int:pk>/unpublish/", views.StaffUnpublishView.as_view(), name="unpublish"),
    path("images/", views.StaffImageUploadView.as_view(), name="image-upload"),
    path("articles/<int:pk>/newsletter/", newsletter_views.StaffArticleNewsletterView.as_view(), name="newsletter"),
    path("articles/<int:pk>/newsletter/test/", newsletter_views.StaffNewsletterTestView.as_view(), name="newsletter-test"),
    path(
        "articles/<int:pk>/newsletter/retry/", newsletter_views.StaffNewsletterRetryView.as_view(), name="newsletter-retry"
    ),
    path("articles/<int:pk>/attachments/", series_views.StaffAttachmentsView.as_view(), name="attachments"),
    path("attachments/<int:pk>/", series_views.StaffAttachmentView.as_view(), name="attachment"),
    path("series/", series_views.StaffSeriesListView.as_view(), name="series-list"),
    path("series/<int:pk>/", series_views.StaffSeriesView.as_view(), name="series"),
    path("series/<int:pk>/stats/", series_views.StaffSeriesStatsView.as_view(), name="series-stats"),
    path("subscribers/", newsletter_views.StaffSubscribersView.as_view(), name="subscribers"),
    path("subscribers/<int:pk>/", newsletter_views.StaffSubscriberView.as_view(), name="subscriber"),
]
