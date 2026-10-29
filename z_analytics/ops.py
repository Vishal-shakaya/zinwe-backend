"""
FR-37 ops log: which environment this is, which release runs, and writing `OpsEvent` rows.

Three writers, all best-effort (a failure to log never breaks the caller):
  - `manage.py ops_event ...`   zinwe-server.sh after deploy / rollback / backup (incl. the off-site copy)
  - `manage.py serve`           one "API started" server row each time the API process starts
  - `error_log.OpsErrorHandler` a logging handler on "django.request": one "error" row per unhandled 500,
                                rate-limited (per route + exception, and per hour), never raises
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Iterable

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import OpsEvent

logger = logging.getLogger("zinwe.analytics")

# /srv/zinwe/<flavour>/releases/<UTC stamp>/backend (zinwe-server.sh layout).
_RELEASE_PATH = re.compile(r"/srv/zinwe/(?P<flavour>[\w-]+)/releases/(?P<stamp>[\w-]+)/backend/?$")


def _base_dir() -> Path:
    return Path(settings.BASE_DIR)


def environment() -> str:
    """
    "prod", "dev" or "local": ZINWE_FLAVOUR when set, else the flavour in the release path
    (/srv/zinwe/<flavour>/releases/...), else "local" (a developer PC).
    """
    configured = (getattr(settings, "ZINWE_ENV", "") or "").strip().lower()
    if configured:
        return configured
    match = _RELEASE_PATH.search(_base_dir().as_posix())
    return match.group("flavour") if match else "local"


def release_info() -> dict[str, str] | None:
    """
    The running release, from the RELEASE file zinwe-server.sh writes next to backend/ (flavour, ref, backend,
    frontend, admin = "<short sha> <subject>", *_sha, deployed_at), plus its stamp. None on a developer PC.
    """
    match = _RELEASE_PATH.search(_base_dir().as_posix())
    path = _base_dir().parent / "RELEASE"
    info: dict[str, str] = {}
    try:
        if path.is_file():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                key, sep, value = line.partition("=")
                if sep and key.strip():
                    info[key.strip()] = value.strip()
    except OSError:
        info = {}
    if match:
        info.setdefault("stamp", match.group("stamp"))
    return info or None


def _pairs(rows: Iterable[Any] | None) -> list[list[str]]:
    out: list[list[str]] = []
    for row in rows or []:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            out.append([str(row[0])[:80], str(row[1])[:500]])
    return out


def _steps(rows: Iterable[Any] | None) -> list[list[str]]:
    out: list[list[str]] = []
    for row in rows or []:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            mark = str(row[1]).lower()
            out.append([str(row[0])[:80], mark if mark in {"ok", "fail", "skip"} else "ok", str(row[2] if len(row) > 2 else "")[:200]])
    return out


def record(
    type: str,
    label: str,
    what: str,
    *,
    result: str = OpsEvent.Result.OK,
    env: str = "",
    who: str = "system",
    result_label: str = "",
    details: Iterable[Any] | None = None,
    steps: Iterable[Any] | None = None,
    source: str = "",
    unresolved: bool | None = None,
    auto_resolve: bool = True,
    at: Any = None,
) -> OpsEvent:
    """
    Write one OpsEvent. A failure is unresolved unless told otherwise (it lights the Logs dot); an OK row of the
    same type + environment resolves the older open failures (last night's failed backup is fixed by tonight's).
    Raises on bad input / DB errors; callers that must never fail use `record_quietly`.
    """
    if type not in OpsEvent.Type.values:
        raise ValueError(f"Unknown type '{type}'. Use one of: {', '.join(OpsEvent.Type.values)}.")
    if result not in OpsEvent.Result.values:
        raise ValueError(f"Unknown result '{result}'. Use one of: {', '.join(OpsEvent.Result.values)}.")
    what = " ".join((what or "").split())
    if not what:
        raise ValueError("Say what happened (--what).")
    env = (env or environment()).strip().lower()[:16]
    if unresolved is None:
        unresolved = result == OpsEvent.Result.FAIL
    with transaction.atomic():
        event = OpsEvent.objects.create(
            at=at or timezone.now(), type=type, label=(label or OpsEvent.Type(type).label)[:40], env=env,
            what=what[:300], who=(who or "system").strip()[:254] or "system", result=result,
            result_label=(result_label or "")[:40], details=_pairs(details), steps=_steps(steps),
            source=(source or "")[:60], unresolved=unresolved,
        )
        if auto_resolve and result == OpsEvent.Result.OK:
            OpsEvent.objects.filter(type=type, env=env, unresolved=True, at__lte=event.at).exclude(pk=event.pk).update(
                unresolved=False, resolved_at=event.at, resolved_by=f"{event.label} {event.at:%Y-%m-%d %H:%M} UTC OK"[:254],
            )
    return event


def record_quietly(*args: Any, **kwargs: Any) -> OpsEvent | None:
    """`record`, but never raises (logging must never fail a deploy, a backup or a request)."""
    try:
        return record(*args, **kwargs)
    except Exception:  # noqa: BLE001
        logger.warning("Could not write an ops event", exc_info=True)
        return None
