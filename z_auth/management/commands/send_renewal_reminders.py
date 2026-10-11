"""`manage.py send_renewal_reminders [--dry-run]`: email due renewal reminders once (see z_auth.renewals).

Safe to run as often as you like (each reminder goes out once per period). Locally the start
script runs it hourly through `run_jobs`; on a server, schedule it hourly (cron / systemd timer).
"""
from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from z_auth.renewals import send_due_reminders


class Command(BaseCommand):
    help = "Send due renewal reminders (7 days / 1 day before a paid plan ends) and expiry notices."

    def add_arguments(self, parser) -> None:  # type: ignore[no-untyped-def]
        parser.add_argument("--dry-run", action="store_true", help="List what would be sent; send nothing.")

    def handle(self, *args: Any, **options: Any) -> None:
        dry = options["dry_run"]
        result = send_due_reminders(dry_run=dry)
        prefix = "Would send" if dry else "Sent"
        for line in result.reminders:
            self.stdout.write(f"{prefix} reminder -> {line}")
        for email in result.expired:
            self.stdout.write(f"{prefix} expiry notice -> {email}")
        self.stdout.write(("DRY RUN: " if dry else "") + result.summary())
