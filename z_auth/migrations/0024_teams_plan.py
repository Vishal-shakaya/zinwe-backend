"""
The Teams plan (owner decision 2026-10-17, J1-02): 25 PCs and 180 days of history, between Startups (10 PCs)
and Businesses (50 PCs), so a 12-PC firm no longer has to jump from Rs 971 to Rs 2,834. Every module, like
every plan since 0022. List Rs 2,499 with 28% off (Rs 1,799); USD $22 a month billed yearly (list $30).
"""
from decimal import Decimal

from django.db import migrations

MODULES = [
    "usb_alerts", "usb_control", "data_transfer", "remote_access", "software_installs", "web",
    "work_time", "app_usage", "team_chat", "alert_to_employee",
]

TEAMS = {
    "name": "Teams",
    "tagline": "For offices of 11 to 25 people",
    "list_price_inr_monthly": Decimal("2499.00"),
    "discount_percent": 28,
    # Historical models skip Plan.save(), so the charged price is set here: 2,499 less 28%, to the rupee.
    "price_inr_monthly": Decimal("1799.00"),
    "price_usd_monthly": Decimal("22.00"),
    "list_price_usd_monthly": Decimal("30.00"),
    "trial_days": 0,
    "is_active": True,
    "is_recommended": False,
    "max_devices": 25,
    "history_days": 180,
    "sort_order": 25,  # Startups 20 < Teams 25 < Businesses 30
    "modules": MODULES,
    "features": [
        "Everything in Startups",
        "Up to 25 PCs for a mid-sized team",
        "180 days of alert and activity history",
        "Email support",
    ],
}

BUSINESSES_FIRST_LINE = ("Everything in Startups", "Everything in Teams")


def forwards(apps, schema_editor):
    Plan = apps.get_model("z_auth", "Plan")
    Plan.objects.update_or_create(code="teams", defaults=TEAMS)
    business = Plan.objects.filter(code="businesses").first()
    if business and business.features and business.features[0] == BUSINESSES_FIRST_LINE[0]:
        business.features = [BUSINESSES_FIRST_LINE[1], *business.features[1:]]
        business.save(update_fields=["features"])


def backwards(apps, schema_editor):
    Plan = apps.get_model("z_auth", "Plan")
    team_plan = Plan.objects.filter(code="teams").first()
    if team_plan is not None:
        # Rows that point at it (subscriptions) keep it; otherwise it goes.
        if team_plan.subscriptions.exists() or team_plan.activations.exists():
            Plan.objects.filter(pk=team_plan.pk).update(is_active=False)
        else:
            team_plan.delete()
    business = Plan.objects.filter(code="businesses").first()
    if business and business.features and business.features[0] == BUSINESSES_FIRST_LINE[1]:
        business.features = [BUSINESSES_FIRST_LINE[0], *business.features[1:]]
        business.save(update_fields=["features"])


class Migration(migrations.Migration):
    dependencies = [("z_auth", "0023_payment_staff_check_tour")]

    operations = [migrations.RunPython(forwards, backwards)]
