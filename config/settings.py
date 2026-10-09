"""
Django settings for the Zinwe backend.

Every value is read from the environment (a local `.env` is loaded if present)
and falls back to a safe *local development* default. See `.env.example`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def env_str(name: str, default: str) -> str:
    """Return an env var as a stripped string, or `default` when unset/blank."""
    value = os.environ.get(name, "").strip()
    return value or default


def env_bool(name: str, default: bool) -> bool:
    """Parse truthy env values: 1/true/yes/on (case-insensitive)."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str) -> list[str]:
    """Parse a comma-separated env var into a list of non-empty items."""
    return [item.strip() for item in env_str(name, default).split(",") if item.strip()]


# --- Core -------------------------------------------------------------------

# INSECURE local-dev fallback. Never used in a real deployment: set DJANGO_SECRET_KEY.
SECRET_KEY = env_str(
    "DJANGO_SECRET_KEY",
    "django-insecure-zinwe-LOCAL-DEV-ONLY-do-not-use-in-production",
)
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "rest_framework",
    "rest_framework.authtoken",
    "corsheaders",
    # Zinwe apps (user-facing names: z-auth, z-anaytics, z-notification, z-main, z-devices)
    "z_auth",
    "z_analytics",
    "z_notification",
    "z_main",
    "z_devices",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Serves /static/ under any server (uvicorn doesn't serve files); right after SecurityMiddleware.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    # CorsMiddleware must sit as high as possible, before CommonMiddleware.
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# --- Database ---------------------------------------------------------------

# Local dev: SQLite. Deployments (dev/prod flavours): PostgreSQL, selected by setting DB_NAME.
# CONN_MAX_AGE stays 0 under ASGI: sync views run on a thread pool, so persistent
# connections would be held per thread and never reused (Django docs).
DB_NAME = env_str("DB_NAME", "")
if DB_NAME:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": DB_NAME,
            "USER": env_str("DB_USER", DB_NAME),
            "PASSWORD": env_str("DB_PASSWORD", ""),
            "HOST": env_str("DB_HOST", "127.0.0.1"),
            "PORT": env_str("DB_PORT", "5432"),
            "CONN_MAX_AGE": 0,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            "CONN_MAX_AGE": 0,
        }
    }

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "zinwe-local",
    }
}

# --- Auth -------------------------------------------------------------------

AUTH_USER_MODEL = "z_auth.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# The test suite creates many users; PBKDF2 at full strength makes it crawl.
# Only ever applies to `manage.py test`.
if sys.argv[1:2] == ["test"]:
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# --- I18N / time ------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

# --- Static -----------------------------------------------------------------

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # Gzip/Brotli-compressed copies on collectstatic. Not the Manifest variant:
    # that needs collectstatic before every run, including tests.
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

# In DEBUG, WhiteNoise serves straight from app static dirs (no collectstatic needed).
# Outside DEBUG it serves STATIC_ROOT, so run `manage.py collectstatic` on deploy.
WHITENOISE_USE_FINDERS = DEBUG
WHITENOISE_AUTOREFRESH = DEBUG

# --- Media (uploads) -------------------------------------------------------
# Payment proofs live here. Never served publicly: there is no MEDIA_URL route;
# staff download through the admin, owners through an authenticated endpoint.
MEDIA_ROOT = Path(env_str("DJANGO_MEDIA_ROOT", str(BASE_DIR / "media")))
MEDIA_URL = "media/"
# Uploads above this size are streamed to a temp file instead of memory.
FILE_UPLOAD_MAX_MEMORY_SIZE = 2_621_440  # 2.5 MB
# Non-file request body limit (JSON, form fields). Files don't count towards it.
DATA_UPLOAD_MAX_MEMORY_SIZE = 2_621_440  # 2.5 MB

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Django REST framework --------------------------------------------------

# Only trust X-Forwarded-For when running behind a known reverse proxy.
ZINWE_TRUST_X_FORWARDED_FOR = env_bool("ZINWE_TRUST_X_FORWARDED_FOR", False)

_renderers = ["rest_framework.renderers.JSONRenderer"]
if DEBUG:
    _renderers.append("rest_framework.renderers.BrowsableAPIRenderer")

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    # Secure by default: public endpoints opt out with AllowAny explicitly.
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": _renderers,
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    # Throttle identity: REMOTE_ADDR unless behind a trusted proxy (then the
    # proxy-appended hop). Without this DRF would trust a client-sent XFF header.
    "NUM_PROXIES": 1 if ZINWE_TRUST_X_FORWARDED_FOR else 0,
    "DEFAULT_THROTTLE_RATES": {
        "early_access": "20/hour",
        "device_enroll": "60/hour",
        "auth_signup": "10/hour",
        "auth_login": "20/hour",
        "auth_otp": "30/hour",
        "payment_submit": "10/hour",
    },
    # Adds `retry_after` (seconds) to every 429 body.
    "EXCEPTION_HANDLER": "z_main.exceptions.exception_handler",
}

# --- CORS -------------------------------------------------------------------

CORS_ALLOWED_ORIGINS = env_list(
    "CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
)

# --- Deployment security ----------------------------------------------------
# Off by default so local dev and plain-HTTP servers work. Turn DJANGO_HTTPS on
# once nginx terminates TLS: secure cookies + trust nginx's X-Forwarded-Proto.

CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", "")
if env_bool("DJANGO_HTTPS", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(env_str("DJANGO_HSTS_SECONDS", "0"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0
SECURE_REFERRER_POLICY = "same-origin"

# --- Email ------------------------------------------------------------------

EMAIL_BACKEND = env_str("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
DEFAULT_FROM_EMAIL = env_str("DEFAULT_FROM_EMAIL", "Zinwe <hello@zinwe.io>")
SERVER_EMAIL = DEFAULT_FROM_EMAIL
EMAIL_HOST = env_str("EMAIL_HOST", "localhost")
EMAIL_PORT = int(env_str("EMAIL_PORT", "25"))
EMAIL_HOST_USER = env_str("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env_str("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", False)
# Folder for django.core.mail.backends.filebased.EmailBackend (browser end-to-end runs read OTPs here).
EMAIL_FILE_PATH = env_str("EMAIL_FILE_PATH", str(BASE_DIR / "sent-emails"))

# --- Zinwe ------------------------------------------------------------------

ZINWE_VERSION = "0.1.0"
ZINWE_TEAM_NOTIFY_EMAIL = env_str("ZINWE_TEAM_NOTIFY_EMAIL", "hello@zinwe.io")
ZINWE_EARLY_ACCESS_TRIAL_DAYS = 90
# Agent heartbeat cadence; a device is "offline" after 3 missed beats.
ZINWE_DEVICE_HEARTBEAT_SECONDS = 60
ZINWE_DEVICE_OFFLINE_AFTER_SECONDS = 3 * ZINWE_DEVICE_HEARTBEAT_SECONDS

# Manual payments (no gateway yet). Shown to owners on the subscribe page.
# Local defaults are PLACEHOLDERS: set the real values in .env.
ZINWE_PAYMENT_INSTRUCTIONS = {
    "upi_id": env_str("ZINWE_PAYMENT_UPI_ID", "placeholder@upi"),
    "payee_name": env_str("ZINWE_PAYMENT_PAYEE_NAME", "SKYTECHNO"),
    "bank_name": env_str("ZINWE_PAYMENT_BANK_NAME", "Placeholder Bank"),
    "account_number": env_str("ZINWE_PAYMENT_ACCOUNT_NUMBER", "000000000000"),
    "ifsc": env_str("ZINWE_PAYMENT_IFSC", "PLHD0000000"),
    "note": env_str("ZINWE_PAYMENT_NOTE", "Use your company name as the payment reference."),
}
ZINWE_PAYMENT_PROOF_MAX_BYTES = 5 * 1024 * 1024

# --- Logging ----------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        "zinwe": {"handlers": ["console"], "level": "INFO" if DEBUG else "WARNING"},
        # Without this, 500 tracebacks are dropped when DEBUG=False (Django only
        # logs to console in DEBUG). On servers they land in the systemd journal.
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
    },
}
