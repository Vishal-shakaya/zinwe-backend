"""Copy the "Book a setup call" sign-ups made before the Forms inbox existed (they keep their real dates)."""
from django.db import migrations

PLATFORMS = {"windows": "Windows", "linux": "Linux", "macos": "macOS"}
CONCERNS = {
    "usb": "USB / removable drives",
    "remote": "Remote access",
    "installs": "Unapproved software installs",
    "email": "Email data leaks",
    "compliance": "Compliance",
}


def backfill(apps, schema_editor):
    EAR = apps.get_model("z_auth", "EarlyAccessRegistration")
    FormSubmission = apps.get_model("z_forms", "FormSubmission")
    if FormSubmission.objects.filter(kind="setup_call").exists():
        return
    FormSubmission.objects.bulk_create(
        FormSubmission(
            kind="setup_call",
            name=r.name,
            email=r.email,
            company=r.company,
            phone=r.phone,
            details={
                "team_size": r.team_size,
                "platforms": [PLATFORMS.get(p, p) for p in r.platforms or []],
                "concerns": [CONCERNS.get(c, c) for c in r.concerns or []],
                "booked_before": False,
            },
            # Only the ones nobody has acted on yet count as new.
            status="new" if r.status == "pending" else "handled",
            ip=r.ip,
            user_agent=r.user_agent,
            created_at=r.created_at,
        )
        for r in EAR.objects.all().order_by("created_at")
    )


class Migration(migrations.Migration):
    dependencies = [
        ("z_forms", "0001_initial"),
        ("z_auth", "0025_quote_snapshot"),
    ]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
