from django.urls import path

from . import newsletter_views, views

app_name = "z_blog"

# Public side, mounted at /api/blog/ (the website's /blog pages, search engines, feed readers).
urlpatterns = [
    path("articles/", views.PublicArticlesView.as_view(), name="articles"),
    path("articles/<slug:slug>/", views.PublicArticleView.as_view(), name="article"),
    path("images/<uuid:key>/", views.PublicImageView.as_view(), name="image"),
    path("sitemap.xml", views.sitemap, name="sitemap"),
    path("rss.xml", views.rss, name="rss"),
    path("newsletter/subscribe/", newsletter_views.SubscribeView.as_view(), name="newsletter-subscribe"),
    path("newsletter/confirm/", newsletter_views.ConfirmView.as_view(), name="newsletter-confirm"),
    path("newsletter/unsubscribe/", newsletter_views.UnsubscribeView.as_view(), name="newsletter-unsubscribe"),
    path(
        "newsletter/unsubscribe/<str:token>/", newsletter_views.one_click_unsubscribe, name="newsletter-one-click"
    ),
]
