from django.apps import AppConfig


class ZMainConfig(AppConfig):
    """Generic, cross-cutting APIs such as health checks (user-facing name: z-main)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "z_main"
    verbose_name = "Main"
