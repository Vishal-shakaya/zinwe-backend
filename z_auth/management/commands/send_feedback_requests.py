"""`manage.py send_feedback_requests [--dry-run]`: email due "How are we doing?" requests once (see z_auth.feedback_requests).

Safe to run as often as you like (an owner is asked at most once per ZINWE_FEEDBACK_REQUEST_EVERY_DAYS).
Locally the start script runs it through `run_jobs`; on a server, schedule it daily (cron / systemd timer).
"""
from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from z_auth.feedback_requests import send_feedback_requests


class Command(BaseCommand):
    help = "Email owners who are due a feedback request (a link to /app/feedback)."

    def add_arguments(self, parser) -> None:  # type: ignore[no-untyped-def]
        parser.add_argument("--dry-run", action="store_true", help="List who would be emailed; send nothing.")

    def handle(self, *args: Any, **options: Any) -> None:
        dry = options["dry_run"]
        result = send_feedback_requests(dry_run=dry)
        prefix = "Would send" if dry else "Sent"
        for email in result.sent:
            self.stdout.write(f"{prefix} feedback request -> {email}")
        self.stdout.write(("DRY RUN: " if dry else "") + result.summary())
