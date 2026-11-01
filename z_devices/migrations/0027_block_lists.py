from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('z_devices', '0026_push_alert_limit'),
    ]

    operations = [
        migrations.AddField(
            model_name='policy',
            name='web_allow_domains',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='policy',
            name='web_upload_warn_bytes',
            field=models.PositiveBigIntegerField(default=1073741824),
        ),
    ]
