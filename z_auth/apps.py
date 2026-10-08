from django.apps import AppConfig


class ZAuthConfig(AppConfig):
    """Accounts, plans and the early-access waitlist (user-facing name: z-auth)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "z_auth"
    verbose_name = "Accounts & plans"
