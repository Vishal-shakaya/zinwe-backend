"""
ASGI entry point for the Zinwe backend.

Served by Uvicorn:

    python manage.py serve                      # dev: 127.0.0.1:8000, auto-reload
    python manage.py serve --workers 4          # prod-style, no reload

or directly: `uvicorn config.asgi:application`. Static files are served by
WhiteNoise middleware, so no separate static handler is needed here.
"""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_asgi_application()
