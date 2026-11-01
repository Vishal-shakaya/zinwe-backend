"""One audience and one draft campaign to start from. No contacts, and nothing can send until staff launch it."""
from __future__ import annotations

from django.db import migrations

from z_marketing import seed


def create(apps, schema_editor) -> None:  # type: ignore[no-untyped-def]
    Audience = apps.get_model("z_marketing", "Audience")
    Campaign = apps.get_model("z_marketing", "Campaign")
    CampaignEmail = apps.get_model("z_marketing", "CampaignEmail")
    audience = Audience.objects.create(**seed.AUDIENCE)
    campaign = Campaign.objects.create(audience=audience, **seed.CAMPAIGN)
    CampaignEmail.objects.bulk_create(
        CampaignEmail(campaign=campaign, n=n, stage=stage, when=when, week=week, subject=subject, body=body)
        for n, stage, when, week, subject, body in seed.EMAILS
    )


class Migration(migrations.Migration):
    dependencies = [("z_marketing", "0001_initial")]

    operations = [migrations.RunPython(create, migrations.RunPython.noop)]
