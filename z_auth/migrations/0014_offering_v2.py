"""
Offering v2 (2026-10-15): plan cards rewritten around what the agent actually does today,
in owner language, as a clear ladder: Free = try it · Startups = protect your data ·
Businesses = protect your data and run your team. "DPDP-ready consent & activity logs"
becomes "Employee consent on every PC" and moves to every plan (consent is required for all
customers). The "Up to N PCs" line is dropped from `features`: the card shows it from `max_devices`.

Note: plans are not enforced in code yet (no PC limit, no module gating) - see .cloude/drive.md.

Data only: taglines + feature lines. Prices, PC limits and flags are untouched (they stay
editable in admin). Reversible: the reverse restores the 0006 texts.
"""
from django.db import migrations

OFFERING = {
    "free": {
        "tagline": "Try Zinwe in your own office",
        "features": [
            "USB drive and file-copy alerts",
            "Plain-English alerts by email",
            "Employee consent on every PC: who agreed, and when",
            "Guided setup",
        ],
    },
    "startups": {
        "tagline": "Full data protection for small teams",
        "features": [
            "Everything in Free forever",
            "Block USB drives or make them read-only",
            "Alerts when files leave by network folder, Outlook or upload",
            "Spot or block remote-access tools like AnyDesk",
            "New software alerts and website blocking",
            "Email support",
        ],
    },
    "businesses": {
        "tagline": "Protect your data and run your team",
        "features": [
            "Everything in Startups",
            "Work hours, breaks and app usage",
            "Chat with your team",
            "Email an alert to the employee involved",
            "Priority support: we answer you first",
        ],
    },
}

PREVIOUS = {
    "free": {
        "tagline": "Basic protection for very small teams",
        "features": ["Up to 3 PCs", "USB and file-copy alerts", "Plain-English alerts by email", "Guided setup"],
    },
    "startups": {
        "tagline": "Every monitoring module for small teams",
        "features": [
            "Up to 10 PCs",
            "Every monitoring module: USB, remote access, software installs, LAN, email",
            "Plain-English alerts",
            "Email support",
        ],
    },
    "businesses": {
        "tagline": "For growing teams that need compliance records",
        "features": ["Up to 50 PCs", "Everything in Startups", "DPDP-ready consent & activity logs", "Priority support"],
    },
}


def _apply(texts):
    def run(apps, schema_editor):
        Plan = apps.get_model("z_auth", "Plan")
        for code, values in texts.items():
            Plan.objects.filter(code=code).update(**values)

    return run


class Migration(migrations.Migration):
    dependencies = [("z_auth", "0013_terms_acceptance")]

    operations = [migrations.RunPython(_apply(OFFERING), _apply(PREVIOUS))]
