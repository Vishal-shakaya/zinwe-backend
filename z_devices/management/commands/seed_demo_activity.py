"""`manage.py seed_demo_activity [--company "Interior bazzar"] [--days 7] [--clear]`

Fills one company's Activity page with realistic sample work days, for demos and local testing:
three demo team members (emails @demo.zinwe.local), each with a demo PC, and for each of the last
N working days a start, a tea break, lunch, sometimes a personal break with a note, and an end.
Past breaks are approved (one denied); today's breaks wait for review. Today stops at "now".

Everything it creates is marked as demo data and removed by `--clear`; real members and PCs are
never touched. Local only: it refuses to run when DEBUG is off unless you pass --force.
"""
from __future__ import annotations

import hashlib
import random
import secrets
from datetime import datetime, time, timedelta
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from z_auth.models import Company, User
from z_devices.models import Device, Member, WorkLog

DEMO_DOMAIN = "demo.zinwe.local"
DEMO_MACHINE_PREFIX = "demo-"

# name, PC, typical start (h, m), personality: how often they take an extra break
PEOPLE = [
    ("Priya Sharma", "ACCOUNTS-PC-01", (9, 0), 0.35),
    ("Rahul Verma", "DESIGN-PC-02", (9, 40), 0.6),
    ("Aisha Khan", "FRONT-DESK", (8, 50), 0.2),
]
EXTRA_BREAKS = [
    ("Personal", "Bank errand"),
    ("Personal", "Picking up my son from school"),
    ("Meeting", "Client call outside"),
    ("Other", "Doctor appointment"),
]

State = WorkLog.State
Approval = WorkLog.Approval


def _slug(name: str) -> str:
    return name.lower().replace(" ", ".")


class Command(BaseCommand):
    help = "Seed (or --clear) sample Activity data: demo members, PCs and work days for one company."

    def add_arguments(self, parser) -> None:  # type: ignore[no-untyped-def]
        parser.add_argument("--company", default="Interior bazzar", help='Company name (default "Interior bazzar").')
        parser.add_argument("--days", type=int, default=7, help="How many days back, including today (default 7).")
        parser.add_argument("--clear", action="store_true", help="Remove this company's demo data and stop.")
        parser.add_argument("--force", action="store_true", help="Allow running with DEBUG off.")

    def handle(self, *args: Any, **options: Any) -> None:
        if not settings.DEBUG and not options["force"]:
            raise CommandError("Demo data is for local use. Pass --force to run with DEBUG off.")
        company = Company.objects.filter(name__iexact=options["company"].strip()).select_related("owner").first()
        if company is None:
            raise CommandError(f'No company named "{options["company"]}".')
        owner = company.owner

        removed = self._clear(owner)
        if options["clear"]:
            self.stdout.write(f"Removed demo data for {company.name}: {removed}.")
            return
        logs = self._seed(owner, max(1, min(options["days"], 31)))
        self.stdout.write(
            f"Seeded {company.name} ({owner.email}): {len(PEOPLE)} demo members, {logs} work-log entries. "
            "Open Activity in the dashboard. Remove with --clear."
        )

    # --- clear ----------------------------------------------------------------

    @transaction.atomic
    def _clear(self, owner: User) -> str:
        devices = Device.objects.filter(owner=owner, machine_id__startswith=DEMO_MACHINE_PREFIX)
        logs = WorkLog.objects.filter(device__in=devices).count()
        n_devices = devices.count()
        devices.delete()  # work logs go with them
        members = Member.objects.filter(owner=owner, email__endswith=f"@{DEMO_DOMAIN}")
        n_members = members.count()
        members.delete()
        return f"{n_members} members, {n_devices} PCs, {logs} work-log entries"

    # --- seed -----------------------------------------------------------------

    @transaction.atomic
    def _seed(self, owner: User, days: int) -> int:
        rng = random.Random(f"zinwe-demo-{owner.pk}")
        now = timezone.localtime()
        today = now.date()
        tz = timezone.get_current_timezone()
        count = 0

        for index, (name, hostname, (h, m), extra_odds) in enumerate(PEOPLE):
            member = Member.objects.create(owner=owner, name=name, email=f"{_slug(name)}@{DEMO_DOMAIN}")
            device = Device.objects.create(
                owner=owner, member=member, hostname=hostname,
                machine_id=f"{DEMO_MACHINE_PREFIX}{_slug(name)}",
                token_hash=hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
                os_version="Windows 11 Pro 24H2", agent_version="0.2.0",
            )
            last_state, worked, on_break = "", 0, 0

            for back in range(days - 1, -1, -1):
                day = today - timedelta(days=back)
                if day.weekday() == 6:  # Sunday off
                    continue
                at = lambda hh, mm, jitter=10: timezone.make_aware(  # noqa: E731
                    datetime.combine(day, time(hh, mm)), tz
                ) + timedelta(minutes=rng.randint(-jitter, jitter))
                plan: list[tuple[str, Any, str, str]] = [(State.WORKING, at(h, m, 15), "", "")]
                plan.append((State.ON_BREAK, at(11, 10), "Tea / coffee", ""))
                plan.append((State.WORKING, plan[-1][1] + timedelta(minutes=rng.randint(10, 20)), "", ""))
                plan.append((State.ON_BREAK, at(13, 0), "Lunch", ""))
                plan.append((State.WORKING, plan[-1][1] + timedelta(minutes=rng.randint(30, 50)), "", ""))
                if rng.random() < extra_odds:
                    reason, note = rng.choice(EXTRA_BREAKS)
                    plan.append((State.ON_BREAK, at(15, 30, 30), reason, note))
                    plan.append((State.WORKING, plan[-1][1] + timedelta(minutes=rng.randint(15, 45)), "", ""))
                plan.append((State.ENDED, at(18, 0, 30), "", ""))

                previous = ""
                for state, when, reason, note in plan:
                    if when > now:
                        break  # today stops at "now"
                    approval = ""
                    decided_at = None
                    if state == State.ON_BREAK:
                        if day == today:
                            approval = Approval.PENDING
                        else:
                            denied = reason == "Personal" and index == 1 and back == 2
                            approval = Approval.DENIED if denied else Approval.APPROVED
                            decided_at = when + timedelta(minutes=30)
                    WorkLog.objects.create(
                        owner=owner, device=device, member=member, state=state, previous_state=previous,
                        at=when, reason=reason, note=note, approval=approval, decided_at=decided_at,
                    )
                    previous = state
                    count += 1
                    if day == today:
                        last_state = state

                if day == today:
                    worked, on_break = _today_totals(device, now)

            # The PC looks like it checked in recently, with today's status.
            device.last_seen_at = now - timedelta(seconds=rng.randint(5, 50)) if last_state in ("working", "on_break") else now - timedelta(hours=1)
            device.work_state = last_state or Device.WorkState.NOT_STARTED
            device.worked_seconds = worked
            device.break_seconds = on_break
            device.work_updated_at = device.last_seen_at
            device.save(update_fields=["last_seen_at", "work_state", "worked_seconds", "break_seconds", "work_updated_at"])
        return count


def _today_totals(device: Device, now: datetime) -> tuple[int, int]:
    """Seconds worked and on break today, from the log (an open stretch runs to now)."""
    entries = list(WorkLog.objects.filter(device=device, at__date=now.date()).order_by("at"))
    worked = breaks = 0
    for i, entry in enumerate(entries):
        end = entries[i + 1].at if i + 1 < len(entries) else (now if entry.state != State.ENDED else entry.at)
        seconds = max(0, int((end - entry.at).total_seconds()))
        if entry.state == State.WORKING:
            worked += seconds
        elif entry.state == State.ON_BREAK:
            breaks += seconds
    return worked, breaks
