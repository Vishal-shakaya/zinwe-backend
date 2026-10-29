"""
`manage.py ops_event` -- write one row to the admin's Logs page (FR-37). zinwe-server.sh calls it after every
deploy, rollback and backup (including the off-site copy); anyone can call it by hand on a server:

    python manage.py ops_event --type backup --label Backup --result fail \\
        --what "Nightly backup · 412 MB · off-site copy failed" \\
        --detail "Local copy=OK · 412 MB" --detail "Off-site copy=B2 upload failed after 3 tries" \\
        --step "Database dump|ok|380 MB" --step "Off-site copy to B2|fail|timed out"

A failure is "unresolved" (it lights the Logs dot and the Dashboard's Needs-you-now) until an OK row of the same
type and environment arrives, or staff mark it resolved on the Logs page. Prints the new row's id.
"""
from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from z_analytics.models import OpsEvent
from z_analytics.ops import record

IST = ZoneInfo("Asia/Kolkata")


def _pair(raw: str) -> list[str]:
    key, sep, value = raw.partition("=")
    if not sep or not key.strip():
        raise CommandError(f'--detail needs "Label=value", got "{raw}".')
    return [key.strip(), value.strip()]


def _step(raw: str) -> list[str]:
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 2 or not parts[0] or parts[1].lower() not in {"ok", "fail", "skip"}:
        raise CommandError(f'--step needs "Label|ok|note" (ok, fail or skip), got "{raw}".')
    return [parts[0], parts[1].lower(), "|".join(parts[2:])]


class Command(BaseCommand):
    help = "Write one ops event (deploy, rollback, backup, server, agent, error) to the admin's Logs page."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--type", required=True, choices=OpsEvent.Type.values)
        parser.add_argument("--label", default="", help='Short label: "Ship", "Promote", "Rollback", "Backup", ...')
        parser.add_argument("--result", default=OpsEvent.Result.OK, choices=OpsEvent.Result.values)
        parser.add_argument("--what", required=True, help="One line: what happened.")
        parser.add_argument("--env", default="", help='"prod", "dev", ... (default: this environment).')
        parser.add_argument("--who", default="system", help='Staff email, or "system".')
        parser.add_argument("--result-label", default="", help='Custom result text, e.g. "Down 4 min".')
        parser.add_argument("--detail", action="append", default=[], metavar="LABEL=VALUE")
        parser.add_argument("--step", action="append", default=[], metavar="LABEL|ok|NOTE")
        parser.add_argument("--source", default="", help='What wrote it, e.g. "zinwe-server".')
        parser.add_argument(
            "--resolved", action="store_true", help="A failure that needs nobody (no Logs dot)."
        )
        parser.add_argument(
            "--keep-open", action="store_true", help="An OK row that does NOT resolve older failures of its type."
        )
        parser.add_argument(
            "--marks-rolled", action="store_true",
            help="A rollback: mark the newest OK deploy of this environment as rolled back.",
        )

    def handle(self, *args: Any, **opts: Any) -> None:
        details = [_pair(d) for d in opts["detail"]]
        steps = [_step(s) for s in opts["step"]]
        try:
            event = record(
                opts["type"], opts["label"], opts["what"], result=opts["result"], env=opts["env"], who=opts["who"],
                result_label=opts["result_label"], details=details, steps=steps, source=opts["source"],
                unresolved=False if opts["resolved"] else None, auto_resolve=not opts["keep_open"],
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        if opts["marks_rolled"]:
            undone = (
                OpsEvent.objects.filter(type=OpsEvent.Type.DEPLOY, env=event.env, result=OpsEvent.Result.OK, at__lt=event.at)
                .exclude(label="Rollback").exclude(pk=event.pk).first()
            )
            if undone is not None:
                undone.result = OpsEvent.Result.ROLLED
                undone.details = [*undone.details, ["Rolled back", f"{timezone.localtime(event.at, IST):%d %b %H:%M} IST · {event.what}"]]
                undone.save(update_fields=["result", "details"])
        self.stdout.write(str(event.pk))
