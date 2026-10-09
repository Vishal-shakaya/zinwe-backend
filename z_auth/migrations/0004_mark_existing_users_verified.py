"""Accounts that existed before email codes count as verified (they were all active)."""
from django.db import migrations
from django.utils import timezone


def mark_verified(apps, schema_editor):
    User = apps.get_model("z_auth", "User")
    User.objects.filter(is_active=True, email_verified_at__isnull=True).update(
        email_verified_at=timezone.now()
    )


class Migration(migrations.Migration):
    dependencies = [("z_auth", "0003_portal_accounts_billing")]

    # Reverse is a no-op: the column itself is dropped when 0003 is reversed.
    operations = [migrations.RunPython(mark_verified, migrations.RunPython.noop)]
