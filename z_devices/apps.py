from django.apps import AppConfig


class ZDevicesConfig(AppConfig):
    """Endpoint agents: enrollment keys, devices, heartbeats (user-facing name: z-devices)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "z_devices"
    verbose_name = "Devices"
