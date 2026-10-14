from __future__ import annotations

from typing import Any

from rest_framework import serializers

from .models import Article, BlogImage
from .services import state


def image_payload(img: BlogImage | None) -> dict[str, Any] | None:
    return {"id": img.id, "url": img.url} if img else None


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

    class Meta:
        model = Article
        fields = [
            "id", "title", "slug", "type", "type_label", "status", "state", "summary", "body_html", "cover",
            "cover_alt", "tags", "seo_title", "meta_description", "author_name", "author_email",
            "reading_minutes", "published_at", "created_at", "updated_at",
        ]

    def get_cover(self, obj: Article) -> dict[str, Any] | None:
        return image_payload(obj.cover_image)

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
