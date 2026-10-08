from django.apps import AppConfig


class ZAnalyticsConfig(AppConfig):
    """Product events and summary metrics (user-facing name: z-anaytics)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "z_analytics"
    verbose_name = "Analytics"
