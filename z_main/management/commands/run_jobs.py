"""`manage.py run_jobs [--interval 3600] [--once]`: the background jobs loop.

Runs every scheduled job now, then again every `--interval` seconds. Today that's renewal
reminders (z_auth.renewals), feedback requests (z_auth.feedback_requests), blog newsletters
(z_blog.newsletter: what a background send left, and issues of scheduled articles that went live), scheduled
reports (z_devices.report_schedules), email retries (z_notification.retry: failed transactional
emails sent again with back-off, rows stuck mid-send marked failed) and
the history clean-up (z_devices.retention: data past each plan's history window + 30 days). A failing job is logged and retried next round; the loop keeps
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
from z_blog.drip import send_due as send_series_emails
from z_blog.guide_followups import send_due as send_guide_followups
from z_blog.newsletter import send_pending as send_newsletters
from z_devices.report_schedules import send_due as send_scheduled_reports
from z_devices.retention import prune_history
from z_notification.retry import run as retry_failed_emails
from z_payments.services import reconcile as check_online_payments

# (label, job): each returns a result with .summary(). One failing job doesn't stop the others.
JOBS = (
    # FIX-008: transactional emails that failed or died mid-send, sent again with back-off (z_notification.retry).
    ("email retries", retry_failed_emails),
    # Razorpay payments whose browser closed before confirming (z_payments): before the renewal reminders, so
    # nobody is reminded to pay for something they just paid.
    ("online payments check", check_online_payments),
    ("renewal reminders", send_due_reminders),
    ("feedback requests", send_feedback_requests),
    ("newsletters", send_newsletters),
    ("series emails", send_series_emails),
    ("guide follow-ups", send_guide_followups),  # after the series: one Zinwe email a day per member
    ("scheduled reports", send_scheduled_reports),  # FR-22: daily / weekly / monthly reports by email
    ("history clean-up", prune_history),
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
                # Long-running loop only: drop stale DB connections between jobs. A --once run is a
                # fresh process (systemd timer) -- and inside a test, closing would kill the test's own
                # connection on PostgreSQL.
                if not options["once"]:
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
