from django.apps import AppConfig


class ZDevicesConfig(AppConfig):
    """Endpoint agents: enrollment keys, devices, heartbeats (user-facing name: z-devices)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "z_devices"
    verbose_name = "Devices"

    def ready(self) -> None:
        from django.db.models.signals import pre_delete

        from .models import Member
        from .pictures import delete_member_file

        # FR-44: a deleted member (directly, or with their owner's account) takes their stored photo with them.
        pre_delete.connect(
            lambda sender, instance, **kwargs: delete_member_file(instance),
            sender=Member,
            weak=False,
            dispatch_uid="z_devices.member_picture_cleanup",
        )
