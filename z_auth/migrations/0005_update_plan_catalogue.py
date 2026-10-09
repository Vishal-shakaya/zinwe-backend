"""
Portal v1 plan catalogue: taglines, PC limits, feature lines, ordering; Pro goes live.

PLACEHOLDER PRICE: Pro's 1999 INR/month is NOT confirmed. The founder must set
the real price (Django admin -> Plans, or a follow-up migration) before launch.
"""
from decimal import Decimal

from django.db import migrations

PLANS = [
    {
        "code": "free",
        "name": "Free",
        "tagline": "Core monitoring for very small teams",
        "price_inr_monthly": Decimal("0"),
        "trial_days": 0,
        "max_devices": 3,
        "features": [
            "Up to 3 PCs",
            "Core activity monitoring",
            "Email alerts",
        ],
        "is_recommended": False,
        "is_active": True,
        "sort_order": 10,
    },
    {
        "code": "early-access",
        "name": "Early access",
        "tagline": "Full plan free for 3 months",
        "price_inr_monthly": Decimal("0"),
        "trial_days": 90,
        "max_devices": 50,
        "features": [
            "Up to 50 PCs",
            "Every monitoring module",
            "Plain-English alerts",
            "Free for 3 months, no card needed",
        ],
        "is_recommended": True,
        "is_active": True,
        "sort_order": 20,
    },
    {
        "code": "pro",
        "name": "Pro",
        "tagline": "Everything, for growing teams",
        # PLACEHOLDER: price to be confirmed by the founder before launch.
        "price_inr_monthly": Decimal("1999.00"),
        "trial_days": 0,
        "max_devices": 50,
        "features": [
            "Up to 50 PCs",
            "Every monitoring module",
            "Plain-English alerts",
            "Priority email support",
        ],
        "is_recommended": False,
        "is_active": True,
        "sort_order": 30,
    },
]


def update_plans(apps, schema_editor):
    Plan = apps.get_model("z_auth", "Plan")
    for plan in PLANS:
        Plan.objects.update_or_create(code=plan["code"], defaults=plan)


def revert_plans(apps, schema_editor):
    """Back to the 0002 state: original names, Pro inactive at 0."""
    Plan = apps.get_model("z_auth", "Plan")
    Plan.objects.filter(code="early-access").update(name="Early access — full plan, 3 months free")
    Plan.objects.filter(code="pro").update(price_inr_monthly=Decimal("0"), is_active=False)


class Migration(migrations.Migration):
    dependencies = [("z_auth", "0004_mark_existing_users_verified")]

    operations = [migrations.RunPython(update_plans, revert_plans)]
