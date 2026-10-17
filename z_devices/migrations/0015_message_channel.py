from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('z_devices', '0014_agent_release'),
    ]

    operations = [
        migrations.AddField(
            model_name='message',
            name='channel',
            field=models.CharField(choices=[('chat', 'Chat (widget)'), ('chat_email', 'Chat + email'), ('email', 'Email only')], default='chat', max_length=12),
        ),
    ]
