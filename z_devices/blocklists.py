"""
The website lists on Rules: six curated lists kept as data files in z_devices/blocklist_data/ with a version
(manifest.json), and what the owner and each PC get from them.

Owner: true counts, and the sites of a list with search and paging. Adult names are masked here, on the server: the
browser only ever gets x•••os.com and a stable id ("h:" + 16 hex characters), and unticking one stores that id, so a
full adult name is in no owner response, in no policy row and in no audit event. Search on a masked list only looks at
what is on screen (plus an exact full name the owner typed themselves, which tells them nothing new).

PC: the resolved set, (lists switched on - allow list) + the owner's own sites. `set_version` names that set, so the
PC fetches it only when it changes. It moves when the owner's choices change or when a new list version ships.

Rebuilding the lists: manage.py rebuild_blocklists (z_devices/blocklist_build.py, sources in blocklist_data/SOURCES.md).
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent / "blocklist_data"
MANIFEST_NAME = "manifest.json"

MASK = "•••"
ID_PREFIX = "h:"
ID_RE = re.compile(r"^h:[0-9a-f]{16}$")
HOST_RE = re.compile(r"^(?=.{3,253}$)(?:[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?\.)+[a-z][a-z0-9-]{1,62}$")
# Second-level endings kept whole when masking, so a name reads x•••os.co.uk rather than giving away the name.
_SHORT_SECOND_LEVEL = {"co", "com", "net", "org", "ac", "gov", "edu", "or", "ne"}

MAX_PAGE_SIZE = 5000
DEFAULT_PAGE_SIZE = 60


@dataclass(frozen=True)
class SiteList:
    key: str
    label: str
    description: str
    masked: bool = False
    # Switched on for the owner when they first choose Block & watch (the dashboard does this; the API stores exactly
    # what it's sent). Social media and video streaming stay off unless the owner wants them.
    default_on: bool = True


LISTS: tuple[SiteList, ...] = (
    SiteList("adult", "Adult content", "Sexual content", masked=True),
    SiteList("gambling", "Gambling", "Betting, casinos and poker"),
    SiteList("malware", "Malware", "Sites that spread viruses or fake downloads"),
    SiteList("phishing", "Phishing & scams", "Fake bank, shop and login pages"),
    SiteList("social", "Social media", "Off unless you want it", default_on=False),
    SiteList("video", "Video streaming", "Off unless you want it", default_on=False),
)
LIST_KEYS: tuple[str, ...] = tuple(item.key for item in LISTS)
LIST_BY_KEY: dict[str, SiteList] = {item.key: item for item in LISTS}


# --- Names --------------------------------------------------------------------------------------------------------


def clean_host(value: str) -> str | None:
    """A bare lower-case host name, without scheme, path, port or a leading www.; None if it isn't one."""
    host = value.strip().lower()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = re.split(r"[/?#]", host, maxsplit=1)[0].rsplit("@", 1)[-1].split(":", 1)[0].strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host if HOST_RE.match(host) else None


def covered_by(host: str, names: Collection[str]) -> str | None:
    """The entry of `names` that is `host` itself or a parent of it (a site includes its subdomains), else None."""
    while True:
        if host in names:
            return host
        _, dot, rest = host.partition(".")
        if not dot or "." not in rest:
            return None
        host = rest


def site_id(domain: str) -> str:
    """The stable id of a hidden name. The same across list rebuilds, so an unticked site stays unticked."""
    return ID_PREFIX + hashlib.sha256(f"zinwe-site:{domain}".encode()).hexdigest()[:16]


def mask(domain: str) -> str:
    """xvideos.com -> x•••os.com; a short name shows less, so the mask always hides most of it."""
    labels = domain.split(".")
    keep = 2 if len(labels) > 2 and len(labels[-1]) == 2 and labels[-2] in _SHORT_SECOND_LEVEL else 1
    name, ending = ".".join(labels[:-keep]), ".".join(labels[-keep:])
    tail = name[-2:] if len(name) >= 6 else name[-1:] if len(name) >= 4 else ""
    return f"{name[0]}{MASK}{tail}.{ending}"


def load_lines(path: Path) -> list[str]:
    """One name per line; blank lines and # comments are skipped."""
    lines = (line.split("#", 1)[0].strip() for line in path.read_text("utf-8").splitlines())
    return [line for line in lines if line]


# --- The files, read once and again when they change ------------------------------------------------------------


@dataclass(frozen=True)
class _Data:
    signature: tuple[Any, ...]
    version: str
    built_at: str
    domains: dict[str, tuple[str, ...]]
    # Every name on a masked list -> its id, and back.
    ids: dict[str, str] = field(default_factory=dict)
    hidden: dict[str, str] = field(default_factory=dict)
    # name -> the lists it is on.
    member: dict[str, tuple[str, ...]] = field(default_factory=dict)


_lock = threading.Lock()
_cache: _Data | None = None


def _read(signature: tuple[Any, ...]) -> _Data:
    manifest = json.loads((DATA_DIR / MANIFEST_NAME).read_text("utf-8"))
    domains = {item.key: tuple(load_lines(DATA_DIR / f"{item.key}.txt")) for item in LISTS}
    ids: dict[str, str] = {}
    member: dict[str, list[str]] = {}
    for item in LISTS:
        for domain in domains[item.key]:
            member.setdefault(domain, []).append(item.key)
            if item.masked:
                ids[domain] = site_id(domain)
    return _Data(
        signature=signature,
        version=manifest["version"],
        built_at=manifest.get("built_at", ""),
        domains=domains,
        ids=ids,
        hidden={value: key for key, value in ids.items()},
        member={key: tuple(value) for key, value in member.items()},
    )


def _data() -> _Data:
    """The lists in memory. A stat per call: a rebuild is picked up without a restart, in every worker."""
    global _cache
    stat = (DATA_DIR / MANIFEST_NAME).stat()
    signature = (str(DATA_DIR), stat.st_mtime_ns, stat.st_size)
    cached = _cache
    if cached is not None and cached.signature == signature:
        return cached
    with _lock:
        if _cache is None or _cache.signature != signature:
            _cache = _read(signature)
        return _cache


def version() -> str:
    return _data().version


# --- Owner --------------------------------------------------------------------------------------------------------


def overview() -> dict[str, Any]:
    """The lists with their true sizes."""
    data = _data()
    return {
        "version": data.version,
        "updated_at": data.built_at,
        "lists": [
            {
                "key": item.key,
                "label": item.label,
                "description": item.description,
                "count": len(data.domains[item.key]),
                "masked": item.masked,
                "default_on": item.default_on,
            }
            for item in LISTS
        ],
    }


def _row(data: _Data, domain: str) -> dict[str, Any]:
    hidden = domain in data.ids
    return {
        "id": data.ids[domain] if hidden else domain,
        "name": mask(domain) if hidden else domain,
        "lists": list(data.member.get(domain, ())),
    }


def sites_page(key: str, query: str = "", page: int = 1, page_size: int = DEFAULT_PAGE_SIZE) -> dict[str, Any]:
    """One page of a list, optionally searched. Adult names come back masked, however it's asked."""
    data = _data()
    domains = data.domains[key]
    needle = query.strip().lower()
    if needle:
        if LIST_BY_KEY[key].masked:
            exact = clean_host(needle)
            domains = tuple(d for d in domains if needle in mask(d) or d == exact)
        else:
            domains = tuple(d for d in domains if needle in d)
    start = (page - 1) * page_size
    chunk = domains[start : start + page_size]
    return {
        "key": key,
        "count": len(domains),
        "page": page,
        "page_size": page_size,
        "has_more": start + page_size < len(domains),
        "masked": LIST_BY_KEY[key].masked,
        "sites": [_row(data, domain) for domain in chunk],
    }


def allow_sites(entries: Iterable[str]) -> list[dict[str, Any]]:
    """The allow list as the owner sees it ("Allowed by you"): id, masked or plain name, and the lists it is on."""
    data = _data()
    rows: list[dict[str, Any]] = []
    for entry in entries:
        domain = data.hidden.get(entry)
        if domain is not None:
            rows.append(_row(data, domain))
        elif ID_RE.match(entry):
            # A hidden name that left the lists in a rebuild. Harmless, and its name is gone with it.
            rows.append({"id": entry, "name": f"Hidden site ({MASK})", "lists": []})
        else:
            rows.append({"id": entry, "name": entry, "lists": list(data.member.get(entry, ()))})
    return rows


def owner_allow(entries: Iterable[str]) -> list[str]:
    """
    The allow list for the owner's browser. Stored entries are safe when saved; this covers a plain name that joined a
    hidden list in a later rebuild.
    """
    ids = _data().ids
    return [ids.get(entry, entry) for entry in entries]


def is_allow_entry(value: str) -> bool:
    return bool(ID_RE.match(value)) or clean_host(value) is not None


def clean_allow(entries: Iterable[str], custom: Iterable[str]) -> list[str]:
    """
    The allow list as stored: a hidden name always as its id (even when the full name was sent), other names plain,
    no repeats. A site that is also one of the owner's own sites comes off: blocking it on purpose wins, so the
    "(lists - allow) + own sites" a PC gets and "never block an allowed site" can both hold.
    """
    data = _data()
    out: dict[str, None] = {}
    for entry in entries:
        if ID_RE.match(entry):
            out[entry] = None
        else:
            host = clean_host(entry)
            if host:
                out[data.ids.get(host, host)] = None
    for name in custom:
        host = clean_host(name)
        if host:
            out.pop(data.ids.get(host, host), None)
    return list(out)


# --- PC -----------------------------------------------------------------------------------------------------------


def device_allow(entries: Iterable[str]) -> list[str]:
    """The allow list a PC gets: real names (a PC needs them; only the owner's screens stay masked)."""
    data = _data()
    return [data.hidden[e] if e in data.hidden else e for e in entries if not ID_RE.match(e) or e in data.hidden]


def resolve(categories: Iterable[str], allow: Iterable[str], custom: Iterable[str]) -> dict[str, list[str]]:
    """What a PC blocks, by list: (lists switched on - allow list) + the owner's own sites, under "custom"."""
    data = _data()
    allow = list(allow)
    allow_ids = {entry for entry in allow if ID_RE.match(entry)}
    allow_names = frozenset(entry for entry in allow if not ID_RE.match(entry))
    wanted = set(categories)
    sites: dict[str, list[str]] = {}
    for key in LIST_KEYS:
        if key in wanted:
            sites[key] = [
                domain
                for domain in data.domains[key]
                if data.ids.get(domain) not in allow_ids and covered_by(domain, allow_names) is None
            ]
    own = {clean_host(name) for name in custom}
    sites["custom"] = sorted(host for host in own if host)
    return sites


def set_version(categories: Iterable[str], allow: Iterable[str], custom: Iterable[str]) -> str:
    """Names one resolved set: the same choices on the same list version give the same value."""
    wanted = sorted(set(categories) & set(LIST_KEYS))
    material = json.dumps([version(), wanted, sorted(set(allow)), sorted(set(custom))], separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest()[:16]
