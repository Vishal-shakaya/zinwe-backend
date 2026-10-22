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


class Series(models.Model):
    """A story told in episodes (e.g. THE LAST FILE): a members-only newsletter series shown at
    /blog/series/<slug>. Its episodes are Articles with `series` set; their full text is for confirmed
    newsletter subscribers only (z_blog.series)."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"

    title = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, unique=True, help_text="The URL: /blog/series/<slug>.")
    tagline = models.CharField(max_length=200, blank=True, help_text="One line under the title.")
    description = models.TextField(max_length=1200, blank=True, help_text="What the series is about (plain text).")
    cover_image = models.ForeignKey(
        BlogImage, null=True, blank=True, on_delete=models.SET_NULL, related_name="cover_of_series"
    )
    cover_alt = models.CharField(max_length=200, blank=True)
    planned_episodes = models.PositiveSmallIntegerField(default=0, help_text="Episodes planned in all (0 = open-ended).")
    cadence = models.CharField(max_length=80, blank=True, help_text='e.g. "A new episode every week".')
    # [{"number": 1, "title": "The Warning", "theme": "Trust"}, ...]
    seasons = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT, db_index=True)
    position = models.PositiveSmallIntegerField(default=0, help_text="Order on the series page (lowest first).")
    # The drip (z_blog.drip): every confirmed subscriber gets the released episodes one by one, from Episode 1,
    # one a day on the chosen weekdays, from `drip_hour` in their own time zone. Episode 1 goes out on confirming.
    drip_enabled = models.BooleanField(default=False, help_text="Email the episodes to every subscriber, one a day.")
    drip_weekdays = models.CharField(
        max_length=20, default="0,1,3,4", help_text="Send days, 0 = Monday … 6 = Sunday (default Mon, Tue, Thu, Fri)."
    )
    drip_hour = models.PositiveSmallIntegerField(default=9, help_text="Local hour (0–23) from which the day's email goes.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position", "-created_at", "-id"]
        verbose_name_plural = "series"

    def __str__(self) -> str:
        return self.title


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
    # A series episode (None = an ordinary blog article). Its body is for subscribers; `teaser_html` is public.
    series = models.ForeignKey(Series, null=True, blank=True, on_delete=models.SET_NULL, related_name="episodes")
    season = models.PositiveSmallIntegerField(default=1)
    episode = models.PositiveSmallIntegerField(null=True, blank=True, help_text="Episode number within the series.")
    teaser_html = models.TextField(blank=True, help_text="The public opening of an episode (cleaned rich text).")
    free_preview = models.BooleanField(default=False, help_text="An episode anyone can read in full (e.g. the pilot).")
    newsletter_subject = models.CharField(max_length=200, blank=True, help_text="Email subject; the title if empty.")
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-published_at", "-updated_at", "-id"]
        indexes = [models.Index(fields=["status", "type", "published_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["series", "episode"], condition=models.Q(series__isnull=False, episode__isnull=False),
                name="z_blog_one_episode_number",
            )
        ]

    def __str__(self) -> str:
        return self.title

    @property
    def tag_list(self) -> list[str]:
        return [t.strip() for t in self.tags.split(",") if t.strip()]

    @property
    def is_published(self) -> bool:
        return self.status == self.Status.PUBLISHED and self.published_at is not None


def attachment_upload_to(instance: "Attachment", filename: str) -> str:
    """blog/files/<yyyy>/<mm>/<uuid>.<ext> (not under a guessable name; served only through the API)."""
    ext = os.path.splitext(filename)[1].lower()[:8]
    now = timezone.now()
    return f"blog/files/{now:%Y}/{now:%m}/{instance.key.hex}{ext}"


class Attachment(models.Model):
    """A file that comes with an article (the case-file PDF, a checklist...). For a series episode it's
    for subscribers only, like the body."""

    key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    article = models.ForeignKey(Article, on_delete=models.CASCADE, related_name="attachments")
    title = models.CharField(max_length=120)
    file = models.FileField(upload_to=attachment_upload_to)
    filename = models.CharField(max_length=120, help_text="Name the download is saved as.")
    content_type = models.CharField(max_length=60)
    size = models.PositiveIntegerField(default=0)
    position = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return f"{self.title} ({self.article_id})"

    @property
    def url(self) -> str:
        """Public path (relative to the API origin); a members-only file also needs the reader key (?k=)."""
        return f"/api/blog/files/{self.key}/"


# --- Newsletter -------------------------------------------------------------------


def new_token() -> str:
    return secrets.token_urlsafe(24)


def new_ref_code() -> str:
    """Short, URL-safe code for a member's share links (?ref=)."""
    return secrets.token_urlsafe(6).replace("-", "x").replace("_", "z")[:8]


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
    # Opens members-only series episodes and their files (kept in the reader's browser, and in the links of
    # every episode email). Separate from `token` so a shared reading link can't unsubscribe anyone.
    access_key = models.CharField(max_length=64, unique=True, default=new_token, editable=False)
    source = models.CharField(max_length=240, blank=True, help_text="Page they signed up on, e.g. /blog/<slug>.")
    # From the browser at sign-up: series emails go out in the reader's morning (z_blog.drip).
    timezone = models.CharField(max_length=64, blank=True, default="")
    # Sharing: every member has a code for "send this to a business owner" links; who brought whom.
    ref_code = models.CharField(max_length=16, unique=True, default=new_ref_code, editable=False)
    referred_by = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="referrals"
    )
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


class SeriesSend(models.Model):
    """One series episode emailed to one subscriber by the drip. (subscriber, episode) is unique, so nobody
    gets an episode twice; `local_date` (the reader's date when it went) keeps it to one email a day."""

    class Status(models.TextChoices):
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"  # tried again on their next send day

    series = models.ForeignKey(Series, on_delete=models.CASCADE, related_name="sends")
    subscriber = models.ForeignKey(NewsletterSubscriber, on_delete=models.CASCADE, related_name="series_sends")
    article = models.ForeignKey(Article, on_delete=models.CASCADE, related_name="series_sends")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SENDING, db_index=True)
    local_date = models.DateField()
    error = models.CharField(max_length=300, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["subscriber", "article"], name="z_blog_one_send_each")]
        indexes = [models.Index(fields=["series", "subscriber", "local_date"])]

    def __str__(self) -> str:
        return f"{self.article_id} -> {self.subscriber_id} ({self.status})"


class SeriesRead(models.Model):
    """A member opened a series episode in full (first and last time). Feeds the admin's per-episode numbers."""

    series = models.ForeignKey(Series, on_delete=models.CASCADE, related_name="reads")
    subscriber = models.ForeignKey(NewsletterSubscriber, on_delete=models.CASCADE, related_name="series_reads")
    article = models.ForeignKey(Article, on_delete=models.CASCADE, related_name="series_reads")
    via_email = models.BooleanField(default=False, help_text="First opened from an episode email link.")
    reads = models.PositiveIntegerField(default=1)
    first_read_at = models.DateTimeField(auto_now_add=True)
    last_read_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["subscriber", "article"], name="z_blog_one_read_row_each")]

    def __str__(self) -> str:
        return f"{self.article_id} read by {self.subscriber_id}"


# --- Guides (e-books) -----------------------------------------------------------------


def guide_upload_to(instance: "Guide", filename: str) -> str:
    """blog/guides/<uuid>.pdf (served only through the members' download, never by its file path)."""
    return f"blog/guides/{uuid.uuid4().hex}.pdf"


class Guide(models.Model):
    """A free e-guide (PDF) at /blog/guides/<slug>: a public page (cover, contents, sample pages) and a download
    for members of the free membership (the newsletter list), the same membership that opens the series."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"

    title = models.CharField(max_length=160)
    slug = models.SlugField(max_length=160, unique=True, help_text="The URL: /blog/guides/<slug>.")
    subtitle = models.CharField(max_length=300, blank=True, help_text="The promise under the title.")
    summary = models.TextField(max_length=300, blank=True, help_text="One or two sentences for cards and search.")
    description = models.TextField(max_length=2000, blank=True, help_text="What the reader gets (plain text, paragraphs).")
    audience = models.CharField(max_length=200, blank=True, help_text='Who it is for, e.g. "Owners and finance staff".')
    cover_image = models.ForeignKey(BlogImage, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    cover_alt = models.CharField(max_length=200, blank=True)
    # Ordered BlogImage ids: a few pages shown on the public page.
    sample_pages = models.JSONField(default=list, blank=True)
    # [{"title": "...", "note": "..."}]: the chapters.
    contents = models.JSONField(default=list, blank=True)
    # [{"value": "30", "label": "Day action plan"}]: the numbers on the cover.
    highlights = models.JSONField(default=list, blank=True)
    feature_line = models.CharField(max_length=300, blank=True, help_text="How Zinwe helps with this (shipped features only).")
    tags = models.CharField(max_length=200, blank=True, help_text="Comma-separated; matches the guide to articles and episodes.")
    campaign = models.CharField(
        max_length=24, blank=True, help_text='Tracking code, e.g. "guide-scams" (UTM campaign and sign-up source).'
    )
    file = models.FileField(upload_to=guide_upload_to, blank=True)
    filename = models.CharField(max_length=120, blank=True, help_text="Name the download is saved as.")
    size = models.PositiveIntegerField(default=0)
    page_count = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT, db_index=True)
    position = models.PositiveSmallIntegerField(default=0, help_text="Order in the library (lowest first).")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position", "-created_at", "-id"]

    def __str__(self) -> str:
        return self.title


class GuideDownload(models.Model):
    """A member downloaded a guide (first and last time, how many times)."""

    guide = models.ForeignKey(Guide, on_delete=models.CASCADE, related_name="downloads")
    subscriber = models.ForeignKey(NewsletterSubscriber, on_delete=models.CASCADE, related_name="guide_downloads")
    downloads = models.PositiveIntegerField(default=1)
    first_at = models.DateTimeField(auto_now_add=True)
    last_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["guide", "subscriber"], name="z_blog_one_guide_download_row")]

    def __str__(self) -> str:
        return f"{self.guide_id} by {self.subscriber_id}"
