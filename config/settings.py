"""
Django settings for the Zinwe backend.

Every value is read from the environment (a local `.env` is loaded if present)
and falls back to a safe *local development* default. See `.env.example`.
"""
from __future__ import annotations

import json
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


def env_json_list(name: str) -> list:
    """Parse an env var holding a JSON array; returns [] when unset or malformed."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    return value if isinstance(value, list) else []


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
    "z_adminapi",
    "z_support",
    "z_blog",
    "z_forms",
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
        # Project templates first: admin/base_site.html carries the Zinwe branding.
        "DIRS": [BASE_DIR / "templates"],
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
        # Failed logins per email + IP (J5-22: offices share one IP behind CGNAT); every login per IP has a
        # much higher ceiling so one address can't spray many accounts.
        "auth_login": "20/hour",
        "auth_login_ip": "300/hour",
        "auth_otp": "30/hour",
        "alert_email": "60/hour",
        "support": "30/hour",
        "payment_submit": "10/hour",
        "feedback": "20/hour",
        "quote_email": "10/hour",
        "quote_public": "120/hour",
        "contact": "10/hour",
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
# The blog newsletter sends from its own domain (authenticated in the same Brevo account), so spam reports on
# bulk mail never touch the reputation of zinwe.io's sign-in codes and invoices. Replies go to the main inbox.
ZINWE_NEWSLETTER_FROM = env_str("ZINWE_NEWSLETTER_FROM", "Zinwe <hello@zinwe.tech>")
ZINWE_NEWSLETTER_REPLY_TO = env_str("ZINWE_NEWSLETTER_REPLY_TO", "hello@zinwe.io")
EMAIL_HOST = env_str("EMAIL_HOST", "localhost")
EMAIL_PORT = int(env_str("EMAIL_PORT", "25"))
EMAIL_HOST_USER = env_str("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env_str("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", False)
# Seconds per SMTP connection: a dead mail server must not hang a request (Django's default is none).
EMAIL_TIMEOUT = int(env_str("EMAIL_TIMEOUT", "20"))
# Fallback SMTP for EMAIL_BACKEND=z_notification.backends.FailoverEmailBackend: used when the primary
# above fails (daily limit, auth/IP block, outage). Gmail sends only as its own address, hence _FROM.
EMAIL_FALLBACK_HOST = env_str("EMAIL_FALLBACK_HOST", "")
EMAIL_FALLBACK_PORT = int(env_str("EMAIL_FALLBACK_PORT", "587"))
EMAIL_FALLBACK_HOST_USER = env_str("EMAIL_FALLBACK_HOST_USER", "")
EMAIL_FALLBACK_HOST_PASSWORD = env_str("EMAIL_FALLBACK_HOST_PASSWORD", "")
EMAIL_FALLBACK_USE_TLS = env_bool("EMAIL_FALLBACK_USE_TLS", True)
EMAIL_FALLBACK_FROM = env_str("EMAIL_FALLBACK_FROM", "")
EMAIL_FAILOVER_PAUSE_SECONDS = int(env_str("EMAIL_FAILOVER_PAUSE_SECONDS", "600"))
# Folder for django.core.mail.backends.filebased.EmailBackend (browser end-to-end runs read OTPs here).
EMAIL_FILE_PATH = env_str("EMAIL_FILE_PATH", str(BASE_DIR / "sent-emails"))

# --- Zinwe ------------------------------------------------------------------

ZINWE_VERSION = "0.1.0"
ZINWE_TEAM_NOTIFY_EMAIL = env_str("ZINWE_TEAM_NOTIFY_EMAIL", "hello@zinwe.io")
# The customer website/dashboard, for links in emails (prod: https://zinwe.io).
ZINWE_APP_URL = env_str("ZINWE_APP_URL", "http://localhost:5173").rstrip("/")
# Version (date) of the Terms, Privacy, Refund, Cookie, Disclaimer and Acceptable Use policies that
# owners accept at sign-up and with each payment. Bump it when the Terms change: every owner on an
# older version is then asked once, in the dashboard, to accept the new one (POST auth/terms/accept/).
ZINWE_LEGAL_VERSION = "2026-10-17"
# Who issues invoices (shown on the printable invoice in Billing). Set the real values in .env.
ZINWE_INVOICE_SELLER = {
    "name": env_str("ZINWE_INVOICE_SELLER_NAME", "SKYTECHNO"),
    "address": env_str("ZINWE_INVOICE_SELLER_ADDRESS", "New Delhi, India"),
    "email": env_str("ZINWE_INVOICE_SELLER_EMAIL", "hello@zinwe.io"),
    "gstin": env_str("ZINWE_INVOICE_SELLER_GSTIN", ""),
}
# GST on invoices (z_auth.pricing). Charged only when the seller GSTIN above is set. Customers in India pay
# 18% on top: CGST + SGST when their state is the seller's, IGST otherwise. Customers outside India pay in
# USD with no GST: an export of services under a Letter of Undertaking (LUT), whose ARN goes on the invoice.
ZINWE_GST = {
    "rate": 18,
    "seller_state": env_str("ZINWE_GST_SELLER_STATE", "Delhi"),
    "sac": env_str("ZINWE_GST_SAC", "998314"),
    "lut_arn": env_str("ZINWE_GST_LUT_ARN", ""),
}
# Skydo: how customers outside India pay (z_auth.billing.payment_instructions). The US account takes
# ACH / wire transfers; a per-customer Skydo payment link (card or US bank) is set in the admin app's
# Subscriptions page. Empty values are hidden.
ZINWE_SKYDO_ACCOUNT = {
    "account_name": env_str("ZINWE_SKYDO_ACCOUNT_NAME", ""),
    "bank_name": env_str("ZINWE_SKYDO_BANK_NAME", ""),
    "account_number": env_str("ZINWE_SKYDO_ACCOUNT_NUMBER", ""),
    "routing_number": env_str("ZINWE_SKYDO_ROUTING_NUMBER", ""),
    "account_type": env_str("ZINWE_SKYDO_ACCOUNT_TYPE", "Checking"),
    "swift": env_str("ZINWE_SKYDO_SWIFT", ""),
}
# The general Skydo payment page (card or US bank), shown when a customer has no payment link of their own.
ZINWE_SKYDO_PAYMENT_URL = env_str("ZINWE_SKYDO_PAYMENT_URL", "")
# Shared quotes (z_auth.quotes): the PDF emailed to the owner and the public /quote/<token> page.
ZINWE_QUOTE_VALID_DAYS = 15
# The internal admin app, for links in team emails (support tickets).
# The built website (zinwe-frontend dist/index.html), used to pre-render /blog pages (z_blog/prerender.py).
ZINWE_FRONTEND_INDEX = env_str("ZINWE_FRONTEND_INDEX", str(BASE_DIR.parent / "zinwe-frontend" / "dist" / "index.html"))
# robots.txt / sitemap.xml (z_main.seo): only the production website is open to search engines. The deploy sets
# this from the flavour's NOINDEX (prod: True); locally and on dev every host answers "Disallow: /".
ZINWE_SEARCH_INDEXING = env_bool("ZINWE_SEARCH_INDEXING", False)
ZINWE_ADMIN_URL = env_str("ZINWE_ADMIN_URL", "http://localhost:5174").rstrip("/")
# Renewal reminders (z_auth.renewals): days before a paid plan ends, each sent once per period.
ZINWE_RENEWAL_REMINDER_DAYS = (7, 1)
# "How are we doing?" emails (z_auth.feedback_requests): first ask after N days, then at most every M days.
ZINWE_FEEDBACK_REQUEST_AFTER_DAYS = 14
ZINWE_FEEDBACK_REQUEST_EVERY_DAYS = 90
# Early access now starts on Free forever (no time-limited trial since v1.2).
ZINWE_EARLY_ACCESS_TRIAL_DAYS = 0
# Agent heartbeat cadence; a device is "offline" after 3 missed beats.
# Fictional email domains of demo accounts (z_main/demo.py, manage.py seed_demo_company): mail to them
# is never delivered and they're left out of the admin dashboard's numbers.
ZINWE_DEMO_EMAIL_DOMAINS = env_list("ZINWE_DEMO_EMAIL_DOMAINS", "kitecrestdigital.com")

ZINWE_DEVICE_HEARTBEAT_SECONDS = 60
ZINWE_DEVICE_OFFLINE_AFTER_SECONDS = 3 * ZINWE_DEVICE_HEARTBEAT_SECONDS
# Where members download the Windows agent; included in the invite email when a member is added.
# Defaults to this environment's own site (ZINWE_APP_URL), so a test site never links to production (J6-01).
ZINWE_AGENT_DOWNLOAD_URL = env_str("ZINWE_AGENT_DOWNLOAD_URL", "") or f"{ZINWE_APP_URL}/download/ZinweAgent.msi"
# Forced upgrade (M12): the minimum agent version allowed to keep working. An agent
# reporting an older version gets `update_required` + the download link on heartbeat,
# and pauses the employee's work until it's updated. Empty = no minimum (nothing forced).
ZINWE_AGENT_MIN_VERSION = env_str("ZINWE_AGENT_MIN_VERSION", "")

# Dashboard notification bars set per environment (optional): a JSON array of
# {id, kind: "version"|"offer"|"discount"|"news"|"maintenance", title, body?, cta_label?, cta_href?, dismissible?}.
# The usual way to push a bar is the admin app's Announcements page (z_auth.Announcement rows, with
# placement / audience / schedule). The "upgrade" bar is derived live from the owner's plan.
# z_auth.announcements validates and drops malformed entries.
ZINWE_ANNOUNCEMENTS = env_json_list("ZINWE_ANNOUNCEMENTS")

# Manual payments (no gateway yet). Shown to owners on the subscribe page.
# Local defaults are PLACEHOLDERS: set the real values in .env.
# PaymentInstructions (portal-v1 §3.7). Real values live in the env only, never in code;
# an empty paypal_url hides the PayPal option (it's for payments from outside India).
ZINWE_PAYMENT_INSTRUCTIONS = {
    "upi_id": env_str("ZINWE_PAYMENT_UPI_ID", ""),
    "payee_name": env_str("ZINWE_PAYMENT_PAYEE_NAME", "SKYTECHNO"),
    "bank_name": env_str("ZINWE_PAYMENT_BANK_NAME", ""),
    "account_type": env_str("ZINWE_PAYMENT_ACCOUNT_TYPE", ""),
    "account_number": env_str("ZINWE_PAYMENT_ACCOUNT_NUMBER", ""),
    "ifsc": env_str("ZINWE_PAYMENT_IFSC", ""),
    "paypal_url": env_str("ZINWE_PAYMENT_PAYPAL_URL", ""),
    "note": env_str("ZINWE_PAYMENT_NOTE", "Use your company name as the payment reference."),
}
# Logo at the top of every HTML email: a public https URL once deployed (e.g.
# https://zinwe.io/brand/zinwe-logo-96.png). Empty (default) = the wordmark in text, same colours.
# Never attach it inline: Gmail accepted and then silently dropped those emails (2026-10-11).
ZINWE_EMAIL_LOGO_URL = env_str("ZINWE_EMAIL_LOGO_URL", "")
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
