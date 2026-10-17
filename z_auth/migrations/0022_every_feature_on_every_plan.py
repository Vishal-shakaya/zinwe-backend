"""
Every plan gets every feature (2026-10-17). Plans now differ by PCs (unchanged: 3 / 10 / 50) and by how much
history the owner sees: Free 7 days, Startups 90 days, Businesses 1 year. Prices are unchanged.

Module gating stays in the code (z_auth.entitlements), so features can be tied to plans again later just by
editing a plan's modules in admin.
"""
from django.db import migrations

MODULES = [
    "usb_alerts", "usb_control", "data_transfer", "remote_access", "software_installs", "web",
    "work_time", "app_usage", "team_chat", "alert_to_employee",
]

PLANS = {
    "free": {
        "history_days": 7,
        "features": [
            "Every Zinwe feature",
            "USB control, file-transfer and remote-access alerts",
            "Website blocking, work hours, app usage and team chat",
            "7 days of alert and activity history",
            "Guided setup",
        ],
    },
    "startups": {
        "history_days": 90,
        "features": [
            "Everything in Free forever",
            "Up to 10 PCs for a small team",
            "90 days of alert and activity history",
            "Email support",
        ],
    },
    "businesses": {
        "history_days": 365,
        "features": [
            "Everything in Startups",
            "Up to 50 PCs for a growing team",
            "1 year of alert and activity history",
            "Priority support: we answer you first",
        ],
    },
}

# Retired plans (inactive) keep their features; give them the longest history so nobody on them loses any.
RETIRED_HISTORY_DAYS = 365


def forwards(apps, schema_editor):
    Plan = apps.get_model("z_auth", "Plan")
    for code, values in PLANS.items():
        Plan.objects.filter(code=code).update(modules=MODULES, **values)
    Plan.objects.exclude(code__in=list(PLANS)).update(modules=MODULES, history_days=RETIRED_HISTORY_DAYS)


def backwards(apps, schema_editor):
    """Back to the module split of migration 0015 (history unlimited)."""
    Plan = apps.get_model("z_auth", "Plan")
    Plan.objects.filter(code="free").update(modules=["usb_alerts"], history_days=0)
    Plan.objects.filter(code="startups").update(
        modules=["usb_alerts", "usb_control", "data_transfer", "remote_access", "software_installs", "web"],
        history_days=0,
    )
    Plan.objects.exclude(code__in=["free", "startups"]).update(modules=MODULES, history_days=0)


class Migration(migrations.Migration):
    dependencies = [("z_auth", "0021_plan_history_days")]

    operations = [migrations.RunPython(forwards, backwards)]
