"""
Plan enforcement (2026-10-15): `Plan.modules` lists what a plan switches on, and
`z_auth.entitlements` enforces it (PC limit, agent policy, data, endpoints).

Seeded to match the offering v2 plan cards (0014). The legacy plans (early-access, pro) were
full plans, so they keep every module. Reversible: the reverse drops the field.
"""
from django.db import migrations, models

FREE = ["usb_alerts"]
STARTUPS = [*FREE, "usb_control", "data_transfer", "remote_access", "software_installs", "web"]
BUSINESSES = [*STARTUPS, "work_time", "app_usage", "team_chat", "alert_to_employee"]

SEED = {
    "free": FREE,
    "startups": STARTUPS,
    "businesses": BUSINESSES,
    "early-access": BUSINESSES,
    "pro": BUSINESSES,
}


def seed(apps, schema_editor):
    Plan = apps.get_model("z_auth", "Plan")
    for code, modules in SEED.items():
        Plan.objects.filter(code=code).update(modules=modules)


class Migration(migrations.Migration):
    dependencies = [("z_auth", "0014_offering_v2")]

    operations = [
        migrations.AddField(
            model_name="plan",
            name="modules",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text=(
                    "What the plan switches on (enforced): usb_alerts, usb_control, data_transfer, remote_access, "
                    "software_installs, web, work_time, app_usage, team_chat, alert_to_employee. See z_auth/entitlements.py."
                ),
            ),
        ),
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
