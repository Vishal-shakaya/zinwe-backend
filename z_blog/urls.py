from django.urls import path

from . import guide_views, newsletter_views, series_views, views

app_name = "z_blog"

# Public side, mounted at /api/blog/ (the website's /blog pages, search engines, feed readers).
urlpatterns = [
    path("articles/", views.PublicArticlesView.as_view(), name="articles"),
    path("articles/<slug:slug>/", views.PublicArticleView.as_view(), name="article"),
    path("images/<uuid:key>/", views.PublicImageView.as_view(), name="image"),
    path("series/", series_views.PublicSeriesListView.as_view(), name="series-list"),
    path("series/<slug:slug>/", series_views.PublicSeriesView.as_view(), name="series"),
    path("series/<slug:slug>/<slug:episode>/", series_views.PublicEpisodeView.as_view(), name="episode"),
    path("files/<uuid:key>/", series_views.PublicFileView.as_view(), name="file"),
    path("guides/", guide_views.PublicGuidesView.as_view(), name="guides"),
    path("guides/<slug:slug>/", guide_views.PublicGuideView.as_view(), name="guide"),
    path("guides/<slug:slug>/download/", guide_views.PublicGuideDownloadView.as_view(), name="guide-download"),
    path("sitemap.xml", views.sitemap, name="sitemap"),
    path("rss.xml", views.rss, name="rss"),
    path("newsletter/subscribe/", newsletter_views.SubscribeView.as_view(), name="newsletter-subscribe"),
    path("newsletter/confirm/", newsletter_views.ConfirmView.as_view(), name="newsletter-confirm"),
    path("newsletter/unsubscribe/", newsletter_views.UnsubscribeView.as_view(), name="newsletter-unsubscribe"),
    path(
        "newsletter/unsubscribe/<str:token>/", newsletter_views.one_click_unsubscribe, name="newsletter-one-click"
    ),
]
