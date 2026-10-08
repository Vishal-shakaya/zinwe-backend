"""
`manage.py serve` -- run the backend on Uvicorn (ASGI).

Defaults come from settings / env so dev needs no flags:

    python manage.py serve                    # 127.0.0.1:8000, reload on in DEBUG
    python manage.py serve --port 9000
    python manage.py serve --host 0.0.0.0 --workers 4 --no-reload

Env overrides: ZINWE_ASGI_HOST, ZINWE_ASGI_PORT, ZINWE_ASGI_WORKERS,
ZINWE_ASGI_RELOAD, ZINWE_ASGI_LOG_LEVEL, ZINWE_FORWARDED_ALLOW_IPS.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

import uvicorn
from django.conf import settings
from django.core.management.base import BaseCommand, CommandParser

ASGI_APP = "config.asgi:application"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


def _reload_dirs(base: Path) -> list[str]:
    """Only project code: config/ and each app (dirs with apps.py), never .venv."""
    dirs = [base / "config"] + [p.parent for p in base.glob("*/apps.py")]
    return sorted({str(d) for d in dirs if d.is_dir()})


class Command(BaseCommand):
    help = "Run the Zinwe backend on the Uvicorn ASGI server."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--host", default=os.environ.get("ZINWE_ASGI_HOST", "127.0.0.1"))
        parser.add_argument("--port", type=int, default=int(os.environ.get("ZINWE_ASGI_PORT", "8000")))
        parser.add_argument(
            "--workers",
            type=int,
            default=int(os.environ.get("ZINWE_ASGI_WORKERS", "1")),
            help="Worker processes. Ignored when --reload is on.",
        )
        parser.add_argument(
            "--reload",
            action=argparse.BooleanOptionalAction,
            default=_env_bool("ZINWE_ASGI_RELOAD", settings.DEBUG),
            help="Restart on code changes (default: on when DEBUG).",
        )
        parser.add_argument(
            "--log-level",
            default=os.environ.get("ZINWE_ASGI_LOG_LEVEL", "info"),
            choices=["critical", "error", "warning", "info", "debug", "trace"],
        )

    def handle(self, *args: Any, **opts: Any) -> None:
        reload: bool = opts["reload"]
        workers: int = 1 if reload else max(1, opts["workers"])
        trust_proxy: bool = getattr(settings, "ZINWE_TRUST_X_FORWARDED_FOR", False)

        mode = "reload" if reload else f"{workers} worker(s)"
        self.stdout.write(
            self.style.SUCCESS(f"Zinwe ASGI on http://{opts['host']}:{opts['port']}/  ({mode}, uvicorn)")
        )

        kwargs: dict[str, Any] = {
            "host": opts["host"],
            "port": opts["port"],
            "log_level": opts["log_level"],
            # Django's ASGIHandler doesn't implement the lifespan protocol.
            "lifespan": "off",
            # Rewrite the client address from X-Forwarded-For only when we've said
            # we're behind a trusted proxy -- same switch the app's IP logic and
            # DRF throttling use, so a client can't spoof its IP via the header.
            "proxy_headers": trust_proxy,
            "forwarded_allow_ips": os.environ.get("ZINWE_FORWARDED_ALLOW_IPS", "127.0.0.1"),
            "server_header": False,
        }
        if reload:
            kwargs.update(reload=True, reload_dirs=_reload_dirs(Path(settings.BASE_DIR)))
        else:
            kwargs["workers"] = workers

        self.stdout.flush()
        uvicorn.run(ASGI_APP, **kwargs)
