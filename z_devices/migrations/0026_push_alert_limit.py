from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('z_devices', '0025_push_subscription'),
    ]

    operations = [
        migrations.AddField(
            model_name='pushsubscription',
            name='last_alert_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
