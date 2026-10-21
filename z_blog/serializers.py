from __future__ import annotations

from typing import Any

from rest_framework import serializers

from .models import Article, Attachment, BlogImage, Series
from .services import state


def image_payload(img: BlogImage | None) -> dict[str, Any] | None:
    return {"id": img.id, "url": img.url} if img else None


def attachment_payload(att: Attachment) -> dict[str, Any]:
    return {
        "id": att.id, "title": att.title, "filename": att.filename, "content_type": att.content_type,
        "size": att.size, "url": att.url,
    }


class ArticleCardSerializer(serializers.ModelSerializer):
    """Public list item."""

    type_label = serializers.CharField(source="get_type_display", read_only=True)
    cover = serializers.SerializerMethodField()
    tags = serializers.ListField(source="tag_list", read_only=True)

    class Meta:
        model = Article
        fields = [
            "slug", "title", "type", "type_label", "summary", "cover", "cover_alt", "tags",
            "author_name", "reading_minutes", "published_at",
        ]

    def get_cover(self, obj: Article) -> dict[str, Any] | None:
        return image_payload(obj.cover_image)


class ArticleDetailSerializer(ArticleCardSerializer):
    """Public article page: body + SEO fields (falling back to title / summary)."""

    seo_title = serializers.SerializerMethodField()
    meta_description = serializers.SerializerMethodField()

    class Meta(ArticleCardSerializer.Meta):
        fields = [*ArticleCardSerializer.Meta.fields, "body_html", "seo_title", "meta_description", "updated_at"]

    def get_seo_title(self, obj: Article) -> str:
        return obj.seo_title or obj.title

    def get_meta_description(self, obj: Article) -> str:
        return obj.meta_description or obj.summary


class StaffArticleSerializer(serializers.ModelSerializer):
    """Everything the editor needs (drafts included)."""

    type_label = serializers.CharField(source="get_type_display", read_only=True)
    cover = serializers.SerializerMethodField()
    author_email = serializers.EmailField(source="author.email", read_only=True, default=None)
    state = serializers.SerializerMethodField()
    series_title = serializers.CharField(source="series.title", read_only=True, default=None)
    series_slug = serializers.CharField(source="series.slug", read_only=True, default=None)
    attachments = serializers.SerializerMethodField()

    class Meta:
        model = Article
        fields = [
            "id", "title", "slug", "type", "type_label", "status", "state", "summary", "body_html", "cover",
            "cover_alt", "tags", "seo_title", "meta_description", "author_name", "author_email",
            "reading_minutes", "published_at", "created_at", "updated_at",
            "series", "series_title", "series_slug", "season", "episode", "teaser_html", "free_preview", "attachments",
            "newsletter_subject",
        ]

    def get_cover(self, obj: Article) -> dict[str, Any] | None:
        return image_payload(obj.cover_image)

    def get_attachments(self, obj: Article) -> list[dict[str, Any]]:
        return [attachment_payload(a) for a in obj.attachments.all()]

    def get_state(self, obj: Article) -> str:
        """draft | scheduled | published (live)."""
        return state(obj)


class PublishInputSerializer(serializers.Serializer):
    """Publish now (no `at`), or schedule for `at` (a future time)."""

    at = serializers.DateTimeField(required=False, allow_null=True)


class ArticleInputSerializer(serializers.Serializer):
    """Editor input. Every field optional on update (PATCH)."""

    title = serializers.CharField(max_length=200)
    slug = serializers.CharField(max_length=220, required=False, allow_blank=True)
    type = serializers.ChoiceField(choices=Article.Type.choices)
    summary = serializers.CharField(max_length=300, required=False, allow_blank=True)
    body_html = serializers.CharField(max_length=200_000, required=False, allow_blank=True, trim_whitespace=False)
    cover_image = serializers.PrimaryKeyRelatedField(queryset=BlogImage.objects.all(), required=False, allow_null=True)
    cover_alt = serializers.CharField(max_length=200, required=False, allow_blank=True)
    tags = serializers.CharField(max_length=400, required=False, allow_blank=True)
    seo_title = serializers.CharField(max_length=70, required=False, allow_blank=True)
    meta_description = serializers.CharField(max_length=170, required=False, allow_blank=True)
    author_name = serializers.CharField(max_length=120, required=False, allow_blank=True)
    series = serializers.PrimaryKeyRelatedField(queryset=Series.objects.all(), required=False, allow_null=True)
    season = serializers.IntegerField(min_value=1, max_value=99, required=False)
    episode = serializers.IntegerField(min_value=1, max_value=999, required=False, allow_null=True)
    teaser_html = serializers.CharField(max_length=50_000, required=False, allow_blank=True, trim_whitespace=False)
    free_preview = serializers.BooleanField(required=False)
    newsletter_subject = serializers.CharField(max_length=200, required=False, allow_blank=True)


class StaffSeriesSerializer(serializers.ModelSerializer):
    cover = serializers.SerializerMethodField()
    episode_count = serializers.SerializerMethodField()
    drip_weekdays = serializers.SerializerMethodField()
    drip_schedule = serializers.SerializerMethodField()
    drip_stats = serializers.SerializerMethodField()

    class Meta:
        model = Series
        fields = [
            "id", "title", "slug", "tagline", "description", "cover", "cover_alt", "planned_episodes", "cadence",
            "seasons", "status", "position", "episode_count", "created_at", "updated_at",
            "drip_enabled", "drip_weekdays", "drip_hour", "drip_schedule", "drip_stats",
        ]

    def get_drip_weekdays(self, obj: Series) -> list[int]:
        from .drip import weekdays

        return sorted(weekdays(obj))

    def get_drip_schedule(self, obj: Series) -> str:
        from .drip import schedule_label

        return schedule_label(obj)

    def get_drip_stats(self, obj: Series) -> dict[str, int]:
        from .drip import stats

        return stats(obj)

    def get_cover(self, obj: Series) -> dict[str, Any] | None:
        return image_payload(obj.cover_image)

    def get_episode_count(self, obj: Series) -> int:
        return obj.episodes.count()


class SeriesInputSerializer(serializers.Serializer):
    """Series editor input. Every field optional on update (PATCH). `seasons`: list or "1 | Title | Theme" lines."""

    title = serializers.CharField(max_length=120)
    slug = serializers.CharField(max_length=140, required=False, allow_blank=True)
    tagline = serializers.CharField(max_length=200, required=False, allow_blank=True)
    description = serializers.CharField(max_length=1200, required=False, allow_blank=True)
    cover_image = serializers.PrimaryKeyRelatedField(queryset=BlogImage.objects.all(), required=False, allow_null=True)
    cover_alt = serializers.CharField(max_length=200, required=False, allow_blank=True)
    planned_episodes = serializers.IntegerField(min_value=0, max_value=999, required=False)
    cadence = serializers.CharField(max_length=80, required=False, allow_blank=True)
    seasons = serializers.JSONField(required=False)
    status = serializers.ChoiceField(choices=Series.Status.choices, required=False)
    position = serializers.IntegerField(min_value=0, max_value=999, required=False)
    drip_enabled = serializers.BooleanField(required=False)
    drip_weekdays = serializers.JSONField(required=False)
    drip_hour = serializers.IntegerField(min_value=0, max_value=23, required=False)
