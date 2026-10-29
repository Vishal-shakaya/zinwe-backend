"""
FR-37: unhandled server errors -> `error` rows on the admin's Logs page.

Its own module because Django configures LOGGING before the apps load: nothing here imports a model until a
record arrives (settings.LOGGING wires it to "django.request").
"""
from __future__ import annotations

import logging
import threading
import time


class OpsErrorHandler(logging.Handler):
    """
    Records unhandled 500s ("django.request" ERROR records with status_code >= 500) as `error` OpsEvents.

    Rate-limited per process: one row per route + exception class every WINDOW seconds (repeats in the window are
    counted and reported on the next row), and at most HOURLY_CAP rows an hour in total. It never raises, never
    writes inside a broken transaction and never records request bodies, query strings or headers.
    """

    WINDOW = 600
    HOURLY_CAP = 20

    def __init__(self, level: int = logging.ERROR) -> None:
        super().__init__(level)
        self._lock = threading.Lock()
        self._last: dict[tuple[str, str, str], float] = {}
        self._repeats: dict[tuple[str, str, str], int] = {}
        self._hour: list[float] = []
        self._local = threading.local()

    def _allow(self, key: tuple[str, str, str], now: float) -> int | None:
        """None = skip this one (counted); else how many repeats were skipped since the last row for this key."""
        with self._lock:
            self._hour = [t for t in self._hour if now - t < 3600]
            last = self._last.get(key)
            if (last is not None and now - last < self.WINDOW) or len(self._hour) >= self.HOURLY_CAP:
                self._repeats[key] = self._repeats.get(key, 0) + 1
                return None
            self._last[key] = now
            self._hour.append(now)
            return self._repeats.pop(key, 0)

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        if getattr(self._local, "busy", False):
            return
        self._local.busy = True
        try:
            status_code = int(getattr(record, "status_code", 0) or 0)
            if status_code < 500:
                return
            request = getattr(record, "request", None)
            method = str(getattr(request, "method", "") or "")[:10]
            match = getattr(request, "resolver_match", None)
            route = getattr(match, "route", "") if match is not None else ""
            path = ("/" + route) if route else str(getattr(request, "path", "") or "")
            path = path[:200]
            exc = record.exc_info[1] if record.exc_info else None
            exc_name = type(exc).__name__ if exc is not None else "Error"
            key = (method, path, exc_name)
            repeats = self._allow(key, time.monotonic())
            if repeats is None:
                return
            from django.db import connection

            if connection.needs_rollback:
                return
            from .models import OpsEvent
            from .ops import record_quietly

            details = [["Exception", exc_name], ["Request", f"{method} {path}".strip()], ["Status", str(status_code)]]
            if repeats:
                details.append(["Repeats", f"{repeats} more since the last row for this route (not logged one by one)"])
            details.append(["Traceback", "In the API log: deploy.ps1 <flavour> logs (journalctl -u zinwe-api@<flavour>)"])
            record_quietly(
                OpsEvent.Type.ERROR, "Error", f"API error · {status_code} on {method} {path} · {exc_name}",
                result=OpsEvent.Result.FAIL, unresolved=False, details=details, source="django.request",
                auto_resolve=False,
            )
        except Exception:  # noqa: BLE001 -- a logging handler must never raise
            pass
        finally:
            self._local.busy = False
