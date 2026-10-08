"""Seed the launch plan catalogue: free, early-access (90-day full plan), pro (inactive)."""
from decimal import Decimal

from django.db import migrations

PLANS = [
    {
        "code": "free",
        "name": "Free",
        "price_inr_monthly": Decimal("0"),
        "trial_days": 0,
        "is_active": True,
    },
    {
        "code": "early-access",
        "name": "Early access — full plan, 3 months free",
        "price_inr_monthly": Decimal("0"),
        "trial_days": 90,
        "is_active": True,
    },
    {
        # TODO(pricing): set the real monthly INR price and activate before launch.
        "code": "pro",
        "name": "Pro",
        "price_inr_monthly": Decimal("0"),
        "trial_days": 0,
        "is_active": False,
    },
]


def seed_plans(apps, schema_editor):
    Plan = apps.get_model("z_auth", "Plan")
    for plan in PLANS:
        Plan.objects.update_or_create(code=plan["code"], defaults=plan)


def unseed_plans(apps, schema_editor):
    Plan = apps.get_model("z_auth", "Plan")
    Plan.objects.filter(code__in=[p["code"] for p in PLANS], activations__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("z_auth", "0001_initial")]

    operations = [migrations.RunPython(seed_plans, unseed_plans)]
