from django.contrib import admin

from .models import Article, BlogImage, NewsletterDelivery, NewsletterIssue, NewsletterSubscriber


@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    """Articles are written in the admin app's Blog tab; this is a read-mostly view."""

    list_display = ("title", "type", "status", "published_at", "author_name", "updated_at")
    list_filter = ("status", "type")
    search_fields = ("title", "summary", "tags")
    prepopulated_fields = {"slug": ("title",)}


@admin.register(BlogImage)
class BlogImageAdmin(admin.ModelAdmin):
    list_display = ("key", "content_type", "size", "uploaded_by", "created_at")
    readonly_fields = ("key", "content_type", "size", "uploaded_by", "created_at")


@admin.register(NewsletterSubscriber)
class NewsletterSubscriberAdmin(admin.ModelAdmin):
    list_display = ("email", "status", "source", "created_at", "confirmed_at", "unsubscribed_at")
    list_filter = ("status",)
    search_fields = ("email", "source")
    readonly_fields = ("token", "created_at", "confirm_sent_at", "confirmed_at", "unsubscribed_at")


class NewsletterDeliveryInline(admin.TabularInline):
    model = NewsletterDelivery
    extra = 0
    fields = ("subscriber", "status", "sent_at", "error")
    readonly_fields = fields
    can_delete = False


@admin.register(NewsletterIssue)
class NewsletterIssueAdmin(admin.ModelAdmin):
    list_display = ("subject", "article", "status", "queued_by", "created_at", "finished_at")
    list_filter = ("status",)
    readonly_fields = ("article", "queued_by", "created_at", "started_at", "finished_at")
    inlines = [NewsletterDeliveryInline]
