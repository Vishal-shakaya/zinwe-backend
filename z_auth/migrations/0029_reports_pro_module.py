"""
FR-22 (owner decision 2026-10-23): advanced reporting is a plan feature. The `reports_pro` module (PDF reports,
emailing a report, scheduled daily / weekly / monthly reports) goes on the paid plans; Free forever keeps the
Reports page and its CSV download. Plan cards say so: Free's "Every Zinwe feature" becomes "Every protection
and team feature", and Startups (which the bigger plans include) lists the new line. Staff can move the module
between plans afterwards in Django admin (Plan.modules).
"""
from django.db import migrations

MODULE = "reports_pro"
PAID = ("startups", "teams", "businesses")
FREE_FIRST = ("Every Zinwe feature", "Every protection and team feature")
STARTUPS_LINE = "PDF reports by email: daily, weekly or monthly"


def forwards(apps, schema_editor):  # type: ignore[no-untyped-def]
    Plan = apps.get_model("z_auth", "Plan")
    for plan in Plan.objects.filter(code__in=PAID):
        if MODULE not in (plan.modules or []):
            plan.modules = [*(plan.modules or []), MODULE]
        if plan.code == "startups" and STARTUPS_LINE not in (plan.features or []):
            features = list(plan.features or [])
            features.insert(min(2, len(features)), STARTUPS_LINE)
            plan.features = features
        plan.save(update_fields=["modules", "features"])
    free = Plan.objects.filter(code="free").first()
    if free and free.features and free.features[0] == FREE_FIRST[0]:
        free.features = [FREE_FIRST[1], *free.features[1:]]
        free.save(update_fields=["features"])


def backwards(apps, schema_editor):  # type: ignore[no-untyped-def]
    Plan = apps.get_model("z_auth", "Plan")
    for plan in Plan.objects.filter(code__in=PAID):
        plan.modules = [m for m in (plan.modules or []) if m != MODULE]
        plan.features = [f for f in (plan.features or []) if f != STARTUPS_LINE]
        plan.save(update_fields=["modules", "features"])
    free = Plan.objects.filter(code="free").first()
    if free and free.features and free.features[0] == FREE_FIRST[1]:
        free.features = [FREE_FIRST[0], *free.features[1:]]
        free.save(update_fields=["features"])


class Migration(migrations.Migration):
    dependencies = [("z_auth", "0028_release_notes")]

    operations = [migrations.RunPython(forwards, backwards)]
