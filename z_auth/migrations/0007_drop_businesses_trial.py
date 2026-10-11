"""
Portal v1.2: no time-limited trial. Free forever is the free way in, so the
Businesses "first 3 months free" trial goes.

- Data: `businesses` and the retired `early-access` plan get `trial_days = 0`
  (the trial code in services/billing stays, dormant, for any old trial rows).
- Schema: new early-access registrations default to 0 trial days.
"""
from django.db import migrations, models

TRIAL_DAYS_BEFORE = {"businesses": 90, "early-access": 90}


def drop_trials(apps, schema_editor):
    Plan = apps.get_model("z_auth", "Plan")
    Plan.objects.filter(code__in=TRIAL_DAYS_BEFORE).update(trial_days=0)


def restore_trials(apps, schema_editor):
    Plan = apps.get_model("z_auth", "Plan")
    for code, days in TRIAL_DAYS_BEFORE.items():
        Plan.objects.filter(code=code).update(trial_days=days)


class Migration(migrations.Migration):

    dependencies = [
        ("z_auth", "0006_v1_1_catalogue_trial_paypal"),
    ]

    operations = [
        migrations.RunPython(drop_trials, restore_trials),
        migrations.AlterField(
            model_name="earlyaccessregistration",
            name="trial_days",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
