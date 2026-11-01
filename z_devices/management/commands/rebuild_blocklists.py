"""
Rebuild the website lists from their public sources and write them into z_devices/blocklist_data/.

    manage.py rebuild_blocklists            malware and phishing (run this every week)
    manage.py rebuild_blocklists --all      adult and gambling too (every three months is enough)
    manage.py rebuild_blocklists --dry-run  show the sizes, write nothing

Run it on a developer PC and ship the changed files with a normal release: they are data in this repo. The manifest
gets a new version only when a list really changed, and PCs pick it up on their next heartbeat after the deploy.
Needs internet (about 90 MB of downloads) and takes a few minutes, most of it the DNS check on malware and phishing.
"""
from __future__ import annotations

import urllib.request
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from z_devices import blocklist_build

ALL_SOURCED = ("adult", "gambling", "malware", "phishing")


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; zinwe-list-builder)"})
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 (fixed https source URLs)
        return response.read()


class Command(BaseCommand):
    help = "Rebuild the website lists. Malware and phishing by default; --all for every list built from sources."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--all", action="store_true", help="Also rebuild adult and gambling.")
        parser.add_argument("--dry-run", action="store_true", help="Show the sizes, write nothing.")
        parser.add_argument("--no-dns", action="store_true", help="Skip the check that malware and phishing names still resolve.")

    def handle(self, *args: Any, **options: Any) -> None:
        result = blocklist_build.build(
            download,
            only=ALL_SOURCED if options["all"] else blocklist_build.STALE_LISTS,
            resolves=None if options["no_dns"] else blocklist_build.dns_resolves,
            write=not options["dry_run"],
        )
        for key, count in result.counts.items():
            mark = " (changed)" if key in result.changed else ""
            self.stdout.write(f"{key}: {count:,} sites{mark}")
        if options["dry_run"]:
            self.stdout.write("Dry run: nothing written.")
        elif result.changed:
            self.stdout.write(self.style.SUCCESS(f"List version {result.version}. Commit blocklist_data/ and ship it."))
        else:
            self.stdout.write("Nothing changed.")
