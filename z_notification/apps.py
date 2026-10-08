from django.apps import AppConfig


class ZNotificationConfig(AppConfig):
    """Outbound notifications over pluggable channels (user-facing name: z-notification)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "z_notification"
    verbose_name = "Notifications"
