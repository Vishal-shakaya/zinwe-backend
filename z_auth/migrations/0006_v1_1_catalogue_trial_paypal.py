"""
Portal v1.1 (portal-v1.md §3.6/§3.7): three-plan catalogue, built-in trial, PayPal.

- Schema: `Subscription.is_trial`; `ManualPayment.method` gains "paypal".
- Data: Free forever / Startups / Businesses (90-day trial, recommended).
  The old `early-access` and `pro` plans are switched off (rows kept: history
  points at them). Prices are flat monthly and editable in admin.

Reversible: the reverse restores the 0005 catalogue and switches the new plans off.
"""
from decimal import Decimal
from importlib import import_module

from django.db import migrations, models

NEW_CODES = ("startups", "businesses")
LEGACY_CODES = ("early-access", "pro")

PLANS = [
    {
        "code": "free",
        "name": "Free forever",
        "tagline": "Basic protection for very small teams",
        "price_inr_monthly": Decimal("0"),
        "trial_days": 0,
        "max_devices": 3,
        "features": [
            "Up to 3 PCs",
            "USB and file-copy alerts",
            "Plain-English alerts by email",
            "Guided setup",
        ],
        "is_recommended": False,
        "is_active": True,
        "sort_order": 10,
    },
    {
        "code": "startups",
        "name": "Startups",
        "tagline": "Every monitoring module for small teams",
        "price_inr_monthly": Decimal("999.00"),
        "trial_days": 0,
        "max_devices": 10,
        "features": [
            "Up to 10 PCs",
            "Every monitoring module: USB, remote access, software installs, LAN, email",
            "Plain-English alerts",
            "Email support",
        ],
        "is_recommended": False,
        "is_active": True,
        "sort_order": 20,
    },
    {
        "code": "businesses",
        "name": "Businesses",
        "tagline": "For growing teams that need compliance records",
        "price_inr_monthly": Decimal("2999.00"),
        "trial_days": 90,
        "max_devices": 50,
        "features": [
            "Up to 50 PCs",
            "Everything in Startups",
            "DPDP-ready consent & activity logs",
            "Priority support",
            # No "First 3 months free" line: the UI shows the trial as a badge only
            # while `trial_available` is true, so it never outlives a used trial.
        ],
        "is_recommended": True,
        "is_active": True,
        "sort_order": 30,
    },
]


def update_catalogue(apps, schema_editor):
    Plan = apps.get_model("z_auth", "Plan")
    for plan in PLANS:
        Plan.objects.update_or_create(code=plan["code"], defaults=plan)
    Plan.objects.filter(code__in=LEGACY_CODES).update(is_active=False, is_recommended=False)


def restore_0005_catalogue(apps, schema_editor):
    """Back to the 0005 catalogue. The new plans are switched off, not deleted
    (re-applying 0006 updates them in place; subscriptions may point at them)."""
    Plan = apps.get_model("z_auth", "Plan")
    previous = import_module("z_auth.migrations.0005_update_plan_catalogue").PLANS
    for plan in previous:
        Plan.objects.update_or_create(code=plan["code"], defaults=plan)
    Plan.objects.filter(code__in=NEW_CODES).update(is_active=False, is_recommended=False)


class Migration(migrations.Migration):
    dependencies = [("z_auth", "0005_update_plan_catalogue")]

    operations = [
        migrations.AddField(
            model_name="subscription",
            name="is_trial",
            field=models.BooleanField(
                default=False, help_text="A free trial of a paid plan (one per account, ever)."
            ),
        ),
        migrations.AlterField(
            model_name="manualpayment",
            name="method",
            field=models.CharField(
                choices=[
                    ("upi", "UPI"),
                    ("bank_transfer", "Bank transfer"),
                    ("paypal", "PayPal"),
                    ("cheque", "Cheque"),
                    ("cash", "Cash"),
                ],
                max_length=20,
            ),
        ),
        migrations.RunPython(update_catalogue, restore_0005_catalogue),
    ]
