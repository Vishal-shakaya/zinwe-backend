"""
Blog articles the Zinwe team writes in the admin app and publishes on the website (/blog), for
SEO and for customers: press releases, tutorials, newsletters, new-release notes, informative posts.
"""
from __future__ import annotations

import os
import secrets
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


def blog_image_upload_to(instance: "BlogImage", filename: str) -> str:
    """blog/<yyyy>/<mm>/<uuid>.<ext> -- never trust the uploaded file name."""
    ext = os.path.splitext(filename)[1].lower()[:8]
    now = timezone.now()
    return f"blog/{now:%Y}/{now:%m}/{instance.key.hex}{ext}"


class BlogImage(models.Model):
    """An image uploaded for an article (cover or inline). Served publicly by its `key`."""

    key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    file = models.FileField(upload_to=blog_image_upload_to)
    content_type = models.CharField(max_length=40)
    size = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"Blog image {self.key}"

    @property
    def url(self) -> str:
        """Public path (relative to the API origin)."""
        return f"/api/blog/images/{self.key}/"


class Article(models.Model):
    class Type(models.TextChoices):
        PRESS_RELEASE = "press_release", "Press release"
        TUTORIAL = "tutorial", "Tutorial"
        NEWSLETTER = "newsletter", "Newsletter"
        NEW_RELEASE = "new_release", "New release"
        INFORMATIVE = "informative", "Informative"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"

    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, unique=True, help_text="The URL: /blog/<slug>.")
    type = models.CharField(max_length=20, choices=Type.choices, default=Type.INFORMATIVE, db_index=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT, db_index=True)
    summary = models.TextField(
        max_length=300, blank=True, help_text="One or two sentences for cards and search results."
    )
    body_html = models.TextField(blank=True, help_text="Cleaned rich text (see z_blog.services.clean_body).")
    cover_image = models.ForeignKey(
        BlogImage, null=True, blank=True, on_delete=models.SET_NULL, related_name="cover_of"
    )
    cover_alt = models.CharField(max_length=200, blank=True)
    tags = models.CharField(max_length=200, blank=True, help_text="Comma-separated, e.g. usb, dpdp, guide.")
    seo_title = models.CharField(max_length=70, blank=True, help_text="Search-result title; the title if empty.")
    meta_description = models.CharField(
        max_length=170, blank=True, help_text="Search-result description; the summary if empty."
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="articles"
    )
    author_name = models.CharField(max_length=120, blank=True, help_text="Shown on the article.")
    reading_minutes = models.PositiveSmallIntegerField(default=1)
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-published_at", "-updated_at", "-id"]
        indexes = [models.Index(fields=["status", "type", "published_at"])]

    def __str__(self) -> str:
        return self.title

    @property
    def tag_list(self) -> list[str]:
        return [t.strip() for t in self.tags.split(",") if t.strip()]

    @property
    def is_published(self) -> bool:
        return self.status == self.Status.PUBLISHED and self.published_at is not None


# --- Newsletter -------------------------------------------------------------------


def new_token() -> str:
    return secrets.token_urlsafe(24)


class NewsletterSubscriber(models.Model):
    """Someone who asked for blog emails on the website. Double opt-in: only `active` ones get mail."""

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting for confirmation"
        ACTIVE = "active", "Subscribed"
        UNSUBSCRIBED = "unsubscribed", "Unsubscribed"

    email = models.EmailField(unique=True)
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.PENDING, db_index=True)
    # Secret for the confirm and unsubscribe links (and the one-click List-Unsubscribe URL).
    token = models.CharField(max_length=64, unique=True, default=new_token, editable=False)
    source = models.CharField(max_length=240, blank=True, help_text="Page they signed up on, e.g. /blog/<slug>.")
    created_at = models.DateTimeField(auto_now_add=True)
    confirm_sent_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    unsubscribed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.email} ({self.status})"


class NewsletterIssue(models.Model):
    """One article emailed to the subscribers (at most once per article)."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"  # waiting to start (or for a scheduled article to go live)
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"

    article = models.OneToOneField(Article, on_delete=models.CASCADE, related_name="newsletter")
    subject = models.CharField(max_length=200)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED, db_index=True)
    queued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"Newsletter: {self.subject} ({self.status})"


class NewsletterDelivery(models.Model):
    """One issue to one subscriber. The (issue, subscriber) pair is unique, so nobody gets it twice."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENDING = "sending", "Sending"  # claimed by a sender
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"  # unsubscribed before their turn

    issue = models.ForeignKey(NewsletterIssue, on_delete=models.CASCADE, related_name="deliveries")
    subscriber = models.ForeignKey(NewsletterSubscriber, on_delete=models.CASCADE, related_name="deliveries")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    error = models.CharField(max_length=300, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["issue", "subscriber"], name="z_blog_one_delivery_each")]

    def __str__(self) -> str:
        return f"{self.issue_id} -> {self.subscriber_id} ({self.status})"
