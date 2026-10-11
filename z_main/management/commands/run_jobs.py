"""`manage.py run_jobs [--interval 3600] [--once]`: the background jobs loop.

Runs every scheduled job now, then again every `--interval` seconds. Today that's renewal
reminders (z_auth.renewals) and feedback requests (z_auth.feedback_requests). A failing job is logged and retried next round; the loop keeps
going. Used by the local start script; a server can run this as a service or schedule the
individual commands with cron instead.
"""
from __future__ import annotations

import time
from typing import Any

from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from z_auth.feedback_requests import send_feedback_requests
from z_auth.renewals import send_due_reminders

# (label, job): each returns a result with .summary(). One failing job doesn't stop the others.
JOBS = (
    ("renewal reminders", send_due_reminders),
    ("feedback requests", send_feedback_requests),
)


class Command(BaseCommand):
    help = "Run Zinwe's scheduled jobs in a loop (renewal reminders, feedback requests)."

    def add_arguments(self, parser) -> None:  # type: ignore[no-untyped-def]
        parser.add_argument("--interval", type=int, default=3600, help="Seconds between rounds (default 3600).")
        parser.add_argument("--once", action="store_true", help="Run one round and exit.")

    def handle(self, *args: Any, **options: Any) -> None:
        interval = max(60, options["interval"])
        names = ", ".join(label for label, _ in JOBS)
        self.stdout.write(f"Zinwe jobs: {names} every {interval // 60} min")
        while True:
            for label, job in JOBS:
                close_old_connections()
                stamp = f"[{timezone.localtime():%Y-%m-%d %H:%M}]"
                try:
                    result = job()
                    self.stdout.write(f"{stamp} {label}: {result.summary()}")
                except Exception as exc:  # noqa: BLE001 -- keep the loop alive; retried next round
                    self.stderr.write(f"{stamp} {label} failed: {exc!r}")
            self.stdout.flush()
            if options["once"]:
                return
            time.sleep(interval)
