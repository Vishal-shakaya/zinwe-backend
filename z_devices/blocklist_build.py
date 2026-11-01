"""
Builds the website lists in z_devices/blocklist_data/ from public sources (see SOURCES.md there for each source's licence).

The lists are curated on purpose: a hosts file can't hold millions of names, and a business can't live with a false
block. So every list keeps only what we can stand behind:

- adult, gambling: names at least two sources agree on (or whose own name says what they are) AND that are in the
  Majestic Million, so only sites people really visit stay. Best ranked first, up to the list's cap.
- malware, phishing: names from sources that allow commercial use, minus anything in the Majestic Million (a popular
  site on a threat feed has been hacked, it isn't a scam), minus what no longer resolves in DNS. Newest first where the
  source gives a date.
- social, video: hand lists, edited here in the repo. The build never touches them.

Everything then goes through safelist.txt, so a common legitimate site can't get in whatever a source says. Fetching
and DNS are passed in, so the tests run without a network.

`publish` is the unattended run on a server: it builds in a scratch folder and only a result that passes its checks
goes live, in one step. Anything else leaves the live lists as they were, and the failure reaches the owner.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import socket
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .blocklists import CURRENT_NAME, DATA_DIR, LIST_KEYS, MANIFEST_NAME, active_dir, clean_host, covered_by, load_lines

Fetch = Callable[[str], bytes]
Resolves = Callable[[str], bool]

PSL_URL = "https://publicsuffix.org/list/public_suffix_list.dat"
MAJESTIC_URL = "https://downloads.majestic.com/majestic_million.csv"
SINFONIETTA_PORN_URL = "https://raw.githubusercontent.com/StevenBlack/hosts/master/extensions/porn/sinfonietta/hosts"
SINFONIETTA_GAMBLING_URL = "https://raw.githubusercontent.com/StevenBlack/hosts/master/extensions/gambling/sinfonietta/hosts"
TIUXO_PORN_URL = "https://raw.githubusercontent.com/StevenBlack/hosts/master/extensions/porn/tiuxo/hosts"
SHADOW_BASE_URL = "https://raw.githubusercontent.com/ShadowWhisperer/BlockLists/master/Lists/"
CERT_PL_URL = "https://hole.cert.pl/domains/v2/domains.csv"
DURABLE_URL = "https://raw.githubusercontent.com/durablenapkin/scamblocklist/master/hosts.txt"
DIGITALSIDE_URL = "https://raw.githubusercontent.com/davidonzo/Threat-Intel/master/lists/latestdomains.txt"

ADULT_CAP = 3000
GAMBLING_CAP = 1000
MALWARE_CAP = 3000
PHISHING_CAP = 4000
SCAM_CAP = 1500  # of PHISHING_CAP
# How far back the CERT Polska register is read: phishing sites rarely last longer than this.
PHISHING_DAYS = 45

# Words that say what a site is on their own; used when only one source lists it.
ADULT_WORDS = ("porn", "xxx", "hentai", "xvideo", "xnxx", "sexcam", "camgirl", "nude", "milf", "boobs", "fetish", "escort", "webcam")
GAMBLING_WORDS = ("casino", "poker", "betting", "sportsbook", "roulette", "blackjack", "slots", "bingo", "lottery", "1xbet", "22bet", "melbet", "mostbet")

STALE_LISTS = ("malware", "phishing")
ALL_SOURCED = ("adult", "gambling", "malware", "phishing")

# Publishing. The rebuild runs weekly; adult and gambling barely move, so they join it every 90 days.
FULL_EVERY = timedelta(days=90)
# A list that falls below this share of its size means a source or DNS went wrong, not that the internet got safer.
MIN_KEEP_SHARE = 0.6
# The popularity check is what keeps a hacked popular site off the threat lists, so a cut-off file must stop the run.
MIN_RANKS = 500_000
KEEP_BUILDS = 4
INPUT_FILES = ("safelist.txt", "gambling-hand.txt")
VERSION_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}\.\d+$")


# --- Reading sources ----------------------------------------------------------------------------------------------


def hosts_of(raw: bytes) -> set[str]:
    """Host names from a hosts file or a plain one-name-per-line list."""
    found: set[str] = set()
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        host = clean_host(line.split()[-1])
        if host:
            found.add(host)
    return found


def cert_pl_recent(raw: bytes, now: datetime, days: int = PHISHING_DAYS) -> list[str]:
    """The CERT Polska register (tab-separated): names still listed, added in the last `days`, newest first."""
    cutoff = now - timedelta(days=days)
    rows: list[tuple[datetime, str]] = []
    reader = csv.reader(io.StringIO(raw.decode("utf-8", "replace")), delimiter="\t")
    next(reader, None)
    for row in reader:
        if len(row) < 3 or (len(row) > 3 and row[3].strip()):  # the fourth column is set once a name is struck off
            continue
        host = clean_host(row[1])
        try:
            added = datetime.fromisoformat(row[2].strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        if host and added >= cutoff:
            rows.append((added, host))
    rows.sort(key=lambda r: (-r[0].timestamp(), r[1]))
    return [host for _, host in rows]


def majestic_ranks(raw: bytes) -> dict[str, int]:
    """domain -> global rank (1 = best) from the Majestic Million CSV."""
    ranks: dict[str, int] = {}
    reader = csv.reader(io.StringIO(raw.decode("utf-8", "replace")))
    next(reader, None)
    for row in reader:
        if len(row) > 2 and row[0].isdigit():
            ranks[row[2].strip().lower()] = int(row[0])
    return ranks


@dataclass(frozen=True)
class PublicSuffixes:
    """Enough of the Public Suffix List to tell a registrable name (pornhub.com) from a shared suffix (github.io)."""

    rules: frozenset[str]
    wild: frozenset[str] = frozenset()
    exceptions: frozenset[str] = frozenset()

    @classmethod
    def parse(cls, raw: bytes) -> PublicSuffixes:
        rules: set[str] = set()
        wild: set[str] = set()
        exceptions: set[str] = set()
        for line in raw.decode("utf-8", "replace").splitlines():
            line = line.strip().lower()
            if not line or line.startswith("//"):
                continue
            if line.startswith("!"):
                exceptions.add(line[1:])
            elif line.startswith("*."):
                wild.add(line[2:])
            else:
                rules.add(line)
        return cls(frozenset(rules), frozenset(wild), frozenset(exceptions))

    def suffix_of(self, host: str) -> str:
        labels = host.split(".")
        for i in range(len(labels)):
            candidate = ".".join(labels[i:])
            if candidate in self.exceptions:
                return ".".join(labels[i + 1:])
            if candidate in self.rules:
                return candidate
            if i + 1 < len(labels) and ".".join(labels[i + 1:]) in self.wild:
                return candidate
        return labels[-1]

    def is_suffix(self, host: str) -> bool:
        return self.suffix_of(host) == host

    def registrable(self, host: str) -> str:
        suffix = self.suffix_of(host)
        if suffix == host:
            return host
        head = host[: -len(suffix) - 1].rsplit(".", 1)[-1]
        return f"{head}.{suffix}"


# --- Choosing -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Guards:
    """What keeps a legitimate site out of every list."""

    safelist: frozenset[str]
    ranks: dict[str, int]
    suffixes: PublicSuffixes

    def allowed(self, host: str) -> bool:
        return not self.suffixes.is_suffix(host) and covered_by(host, self.safelist) is None


def well_known(
    sources: Iterable[set[str]], words: tuple[str, ...], guards: Guards, cap: int
) -> list[str]:
    """Adult, gambling: agreed by two sources (or named for what they are), ranked in the Majestic Million."""
    votes: dict[str, int] = {}
    for source in sources:
        for host in {guards.suffixes.registrable(h) for h in source}:
            votes[host] = votes.get(host, 0) + 1
    chosen = [
        host
        for host, count in votes.items()
        if host in guards.ranks
        and guards.allowed(host)
        and (count >= 2 or any(word in host for word in words))
    ]
    chosen.sort(key=lambda host: (guards.ranks[host], host))
    return chosen[:cap]


def even_sample(hosts: Iterable[str]) -> list[str]:
    """A source with no dates, in an order that doesn't favour the start of the alphabet and barely moves between builds."""
    return sorted(hosts, key=lambda host: hashlib.sha256(host.encode()).digest())


def threats(
    ordered: Iterable[str], guards: Guards, resolves: Resolves | None, cap: int, *, workers: int = 48
) -> list[str]:
    """Malware, phishing: keep the given order, drop popular and safelisted names, then keep what still resolves."""
    seen: set[str] = set()
    picked: list[str] = []
    for host in ordered:
        if host in seen:
            continue
        seen.add(host)
        if guards.suffixes.registrable(host) in guards.ranks or not guards.allowed(host):
            continue
        picked.append(host)
    if resolves is None:
        return picked[:cap]
    out: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # A slice at a time, so a long source stops being looked up once the cap is reached.
        for start in range(0, len(picked), workers * 20):
            batch = picked[start : start + workers * 20]
            out.extend(host for host, alive in zip(batch, pool.map(resolves, batch)) if alive)
            if len(out) >= cap:
                break
    return out[:cap]


def dns_resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return True
    except (OSError, UnicodeError):
        return False


# --- The whole build ----------------------------------------------------------------------------------------------


@dataclass
class BuildResult:
    version: str
    counts: dict[str, int]
    changed: list[str] = field(default_factory=list)


def build(
    fetch: Fetch,
    *,
    only: tuple[str, ...] = STALE_LISTS,
    data_dir: Path = DATA_DIR,
    resolves: Resolves | None = dns_resolves,
    now: datetime | None = None,
    write: bool = True,
    min_ranks: int = 0,
) -> BuildResult:
    """Rebuild the lists in `only`. Lists not named keep their file; the manifest always covers all six."""
    now = now or datetime.now(timezone.utc)
    guards = Guards(
        safelist=frozenset(load_lines(data_dir / "safelist.txt")),
        ranks=majestic_ranks(fetch(MAJESTIC_URL)),
        suffixes=PublicSuffixes.parse(fetch(PSL_URL)),
    )
    if len(guards.ranks) < min_ranks:
        raise ValueError(f"The Majestic Million came back with only {len(guards.ranks):,} sites. Nothing was written.")
    fresh: dict[str, list[str]] = {}
    if "adult" in only:
        fresh["adult"] = well_known(
            [hosts_of(fetch(SINFONIETTA_PORN_URL)), hosts_of(fetch(TIUXO_PORN_URL)), hosts_of(fetch(SHADOW_BASE_URL + "Adult"))],
            ADULT_WORDS, guards, ADULT_CAP,
        )
    if "gambling" in only:
        chosen = well_known(
            [hosts_of(fetch(SINFONIETTA_GAMBLING_URL)), hosts_of(fetch(SHADOW_BASE_URL + "Gambling"))],
            GAMBLING_WORDS, guards, GAMBLING_CAP,
        )
        hand = [h for h in load_lines(data_dir / "gambling-hand.txt") if guards.allowed(h)]
        fresh["gambling"] = [*hand, *(h for h in chosen if h not in hand)][:GAMBLING_CAP]
    if "malware" in only:
        fresh["malware"] = threats(
            [*sorted(hosts_of(fetch(DIGITALSIDE_URL))), *even_sample(hosts_of(fetch(SHADOW_BASE_URL + "Malware")))],
            guards, resolves, MALWARE_CAP,
        )
    if "phishing" in only:
        # Fresh fake logins first, then fake shops and scam sites, which live longer.
        fresh["phishing"] = [
            *threats(cert_pl_recent(fetch(CERT_PL_URL), now), guards, resolves, PHISHING_CAP - SCAM_CAP),
            *threats(
                [*sorted(hosts_of(fetch(DURABLE_URL))), *even_sample(hosts_of(fetch(SHADOW_BASE_URL + "Scam")))],
                guards, resolves, SCAM_CAP,
            ),
        ]

    # One list per site, in list order, so the totals on Rules add up. A list not rebuilt here keeps its names.
    taken: set[str] = set()
    for key in LIST_KEYS:
        if key in fresh:
            fresh[key] = [host for host in dict.fromkeys(fresh[key]) if host not in taken]
            taken.update(fresh[key])
        elif (data_dir / f"{key}.txt").exists():
            taken.update(load_lines(data_dir / f"{key}.txt"))

    changed = []
    for key, hosts in fresh.items():
        if not hosts:
            # A source that came back empty must never wipe a list.
            raise ValueError(f"The {key} list came out empty. Nothing was written.")
        hosts = sorted(set(hosts))
        path = data_dir / f"{key}.txt"
        if not path.exists() or load_lines(path) != hosts:
            changed.append(key)
        if write:
            path.write_text("\n".join(hosts) + "\n", encoding="utf-8", newline="\n")

    manifest_path = data_dir / MANIFEST_NAME
    old: dict[str, Any] = json.loads(manifest_path.read_text("utf-8")) if manifest_path.exists() else {}
    version = next_version(old.get("version", ""), now)
    counts = {key: len(fresh[key]) if key in fresh else _count(data_dir / f"{key}.txt") for key in LIST_KEYS}
    if changed and write:
        built_at = now.replace(microsecond=0).isoformat()
        full_built_at = built_at if set(ALL_SOURCED) <= set(fresh) else old.get("full_built_at", old.get("built_at", ""))
        manifest = {
            "version": version,
            "built_at": built_at,
            "full_built_at": full_built_at,
            "lists": {
                key: {
                    "count": counts[key],
                    "sha256": hashlib.sha256((data_dir / f"{key}.txt").read_bytes()).hexdigest(),
                }
                for key in LIST_KEYS
            },
        }
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    return BuildResult(version if changed else old.get("version", ""), counts, changed)


def _count(path: Path) -> int:
    return len(load_lines(path)) if path.exists() else 0


# --- Publishing, for the weekly timer -----------------------------------------------------------------------------


def full_rebuild_due(data_dir: Path, now: datetime) -> bool:
    manifest = json.loads((data_dir / MANIFEST_NAME).read_text("utf-8"))
    last = manifest.get("full_built_at") or manifest.get("built_at")
    return not last or now - datetime.fromisoformat(last) >= FULL_EVERY


def check_rebuild(built: Path, before: Path, rebuilt: tuple[str, ...]) -> None:
    """Refuse a rebuild that lost too much of a list. Raises ValueError saying which."""
    for key in rebuilt:
        old, new = _count(before / f"{key}.txt"), _count(built / f"{key}.txt")
        if new < old * MIN_KEEP_SHARE:
            raise ValueError(f"The {key} list fell from {old:,} to {new:,} sites. Nothing was published.")


def publish(
    fetch: Fetch,
    root: Path,
    *,
    full: bool = False,
    resolves: Resolves | None = dns_resolves,
    now: datetime | None = None,
) -> BuildResult:
    """Rebuild into root/<version>/ and point root/current.txt at it. Builds are never edited once live."""
    now = now or datetime.now(timezone.utc)
    root.mkdir(parents=True, exist_ok=True)
    for leftover in root.glob(".stage-*"):
        shutil.rmtree(leftover, ignore_errors=True)
    before = active_dir()
    only = ALL_SOURCED if full or full_rebuild_due(before, now) else STALE_LISTS
    stage = root / f".stage-{now:%Y%m%d%H%M%S}"
    stage.mkdir()
    try:
        for source in before.iterdir():
            if source.is_file():
                shutil.copy2(source, stage / source.name)
        # What the build reads comes from this release, so an edited safelist applies from the next run.
        for name in INPUT_FILES:
            shutil.copy2(DATA_DIR / name, stage / name)
        result = build(fetch, only=only, data_dir=stage, resolves=resolves, now=now, min_ranks=MIN_RANKS)
        if not result.changed:
            return result
        check_rebuild(stage, before, only)
        target = root / result.version
        if target.exists():
            shutil.rmtree(target)
        stage.rename(target)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    pointer = root / f"{CURRENT_NAME}.tmp"
    pointer.write_text(result.version + "\n", encoding="utf-8")
    os.replace(pointer, root / CURRENT_NAME)
    _prune(root, result.version)
    return result


def _prune(root: Path, live: str) -> None:
    builds = [item for item in root.iterdir() if item.is_dir() and VERSION_RE.match(item.name) and item.name != live]
    builds.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    for old in builds[KEEP_BUILDS - 1 :]:
        shutil.rmtree(old, ignore_errors=True)


def next_version(previous: str, now: datetime) -> str:
    """2026.11.01.1, then .2 for a second build the same day."""
    day = now.strftime("%Y.%m.%d")
    if previous.startswith(f"{day}."):
        try:
            return f"{day}.{int(previous.rsplit('.', 1)[1]) + 1}"
        except ValueError:
            pass
    return f"{day}.1"
