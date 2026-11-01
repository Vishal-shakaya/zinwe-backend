"""
Rebuild the website lists from their public sources and write them into z_devices/blocklist_data/.

    manage.py rebuild_blocklists            malware and phishing (run this every week)
    manage.py rebuild_blocklists --all      adult and gambling too (every three months is enough)
    manage.py rebuild_blocklists --dry-run  show the sizes, write nothing
    manage.py rebuild_blocklists --publish  what the servers run: goes live on this server, no release needed

A weekly timer on each server runs --publish (zinwe-server blocklists), which adds adult and gambling every 90 days,
writes outside the release, and goes live only if the result passes its checks. PCs pick it up on their next heartbeat.

Without --publish it writes into this repo's blocklist_data/ on a developer PC, for shipping with a normal release.
The manifest gets a new version only when a list really changed.
Needs internet (about 90 MB of downloads) and takes a few minutes, most of it the DNS check on malware and phishing.
"""
from __future__ import annotations

import http.client
import time
import urllib.request
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from z_devices import blocklist_build, blocklists

TRIES = 3


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; zinwe-list-builder)"})
    for attempt in range(1, TRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 (fixed https source URLs)
                return response.read()
        except (OSError, http.client.HTTPException):
            if attempt == TRIES:
                raise
            time.sleep(20 * attempt)
    raise AssertionError("unreachable")


class Command(BaseCommand):
    help = "Rebuild the website lists. Malware and phishing by default; --all for every list built from sources."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--all", action="store_true", help="Also rebuild adult and gambling.")
        parser.add_argument("--dry-run", action="store_true", help="Show the sizes, write nothing.")
        parser.add_argument("--no-dns", action="store_true", help="Skip the check that malware and phishing names still resolve.")
        parser.add_argument("--publish", action="store_true", help="Check the result and put it live on this server.")

    def handle(self, *args: Any, **options: Any) -> None:
        resolves = None if options["no_dns"] else blocklist_build.dns_resolves
        if options["publish"]:
            self.publish(full=options["all"], resolves=resolves)
            return
        result = blocklist_build.build(
            download,
            only=blocklist_build.ALL_SOURCED if options["all"] else blocklist_build.STALE_LISTS,
            resolves=resolves,
            write=not options["dry_run"],
        )
        self.show(result)
        if options["dry_run"]:
            self.stdout.write("Dry run: nothing written.")
        elif result.changed:
            self.stdout.write(self.style.SUCCESS(f"List version {result.version}. Commit blocklist_data/ and ship it."))
        else:
            self.stdout.write("Nothing changed.")

    def publish(self, *, full: bool, resolves: blocklist_build.Resolves | None) -> None:
        root = blocklists.live_dir()
        if root is None:
            raise CommandError("ZINWE_BLOCKLIST_DIR isn't set, so there's nowhere to publish to.")
        try:
            result = blocklist_build.publish(download, root, full=full, resolves=resolves)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.show(result)
        self.stdout.write(f"Published {result.version}." if result.changed else f"Nothing changed. Still {result.version}.")

    def show(self, result: blocklist_build.BuildResult) -> None:
        for key, count in result.counts.items():
            mark = " (changed)" if key in result.changed else ""
            self.stdout.write(f"{key}: {count:,} sites{mark}")
