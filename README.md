# Zinwe backend

Django + Django REST framework API for **Zinwe** (SMB data-protection / activity
monitoring, by SKYTECHNO, New Delhi). Pre-launch: it currently powers the
website's early-access waitlist (full plan free for 3 months = 90 days), basic
accounts/plans, notifications and analytics.

## Apps

Python packages can't contain hyphens, so the app names map like this:

| Name used in planning | Python package   | What it owns |
|-----------------------|------------------|--------------|
| z-auth                | `z_auth`         | `User` (email login), `Plan`, `PlanActivation`, `EarlyAccessRegistration`; register/login/me; plans |
| z-anaytics            | `z_analytics`    | `Event` log, `track()`, admin summary endpoint |
| z-notification        | `z_notification` | Channel registry (email today), `Notification` log, `notify()`, templates |
| z-main                | `z_main`         | Health check and the home for generic, cross-app APIs/helpers |
| z-devices             | `z_devices`      | `EnrollmentKey`, `Device` (PCs running the Windows agent), `Policy` (USB rule), `DeviceEvent` (activity feed); enroll/heartbeat/policy/events; `Device <token>` auth |

Business logic lives in each app's `services.py`; views only validate (serializers)
and call services. Side effects (analytics + emails) are explicit calls in
`z_auth/services.py`, scheduled with `transaction.on_commit` (no hidden signals).

## Setup (Windows, Python 3.12)

```powershell
cd D:\Programming\zinwe\backend\zinwe-backend
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env          # optional; every setting has a local default
.venv\Scripts\python.exe manage.py migrate
.venv\Scripts\python.exe manage.py serve          # Uvicorn (ASGI) on 127.0.0.1:8000, auto-reload
```

## Running (ASGI / Uvicorn)

The app runs on **Uvicorn** via `config/asgi.py`. `manage.py serve` wraps it with
project defaults, so dev needs no flags:

```powershell
.venv\Scripts\python.exe manage.py serve                                  # dev: reload on (DEBUG)
.venv\Scripts\python.exe manage.py serve --port 9000
.venv\Scripts\python.exe manage.py serve --host 0.0.0.0 --no-reload --workers 4   # prod-style
# equivalent raw command:
.venv\Scripts\uvicorn.exe config.asgi:application --host 127.0.0.1 --port 8000 --lifespan off
```

- **Reload** watches only `config/` and the app folders (not `.venv`).
- **Static files** (admin, DRF pages) are served by WhiteNoise middleware, because
  Uvicorn doesn't serve files. In DEBUG they come straight from the apps; outside
  DEBUG run `manage.py collectstatic` on deploy (served from `staticfiles/`, gzip/brotli).
- **`lifespan` is off**: Django's ASGI handler doesn't implement it.
- **Proxy headers** (`X-Forwarded-For`) are honoured only when
  `ZINWE_TRUST_X_FORWARDED_FOR=True`, from IPs in `ZINWE_FORWARDED_ALLOW_IPS`. It's the
  same switch the app's IP logging and rate limiting use, so clients can't spoof their IP.
- **DB connections**: `CONN_MAX_AGE=0`. Under ASGI, sync views run on a thread pool,
  so persistent connections wouldn't be reused.
- **Views stay sync** (DRF). Django runs them in a thread under ASGI, so nothing
  needed to change, and async views/websockets can be added later on the same server.
- `runserver` still works for quick debugging, but it's WSGI; use `serve` to run what
  production runs.

Tests and checks:

```powershell
.venv\Scripts\python.exe manage.py test
.venv\Scripts\python.exe manage.py check
```

### Local dev superuser (LOCAL DEV ONLY)

```powershell
$env:DJANGO_SUPERUSER_EMAIL="admin@zinwe.local"; $env:DJANGO_SUPERUSER_PASSWORD="zinwe-admin-123"
.venv\Scripts\python.exe manage.py createsuperuser --noinput
```

Log in at http://127.0.0.1:8000/admin/ with `admin@zinwe.local` / `zinwe-admin-123`.
Never create this account on any shared or deployed environment.

## Configuration (env vars, `.env` is loaded if present)

| Variable | Default |
|----------|---------|
| `DJANGO_SECRET_KEY` | insecure dev key (**must** be set outside local dev) |
| `DJANGO_DEBUG` | `True` |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` |
| `EMAIL_BACKEND` | console backend (emails print in the server terminal) |
| `DEFAULT_FROM_EMAIL` | `Zinwe <hello@zinwe.io>` |
| `ZINWE_TEAM_NOTIFY_EMAIL` | `hello@zinwe.io` (receives new sign-up alerts) |
| `ZINWE_TRUST_X_FORWARDED_FOR` | `False` (set `True` only behind a trusted reverse proxy) |
| `EMAIL_HOST/PORT/HOST_USER/HOST_PASSWORD/USE_TLS` | SMTP settings when using the SMTP backend |
| `ZINWE_ASGI_HOST` / `ZINWE_ASGI_PORT` | `127.0.0.1` / `8000` (`manage.py serve`) |
| `ZINWE_ASGI_WORKERS` | `1` (ignored while reload is on) |
| `ZINWE_ASGI_RELOAD` | same as `DJANGO_DEBUG` |
| `ZINWE_ASGI_LOG_LEVEL` | `info` |
| `ZINWE_FORWARDED_ALLOW_IPS` | `127.0.0.1` (proxy IPs trusted for `X-Forwarded-*`) |

SQLite (`db.sqlite3`) locally, `TIME_ZONE=Asia/Kolkata`, `USE_TZ=True`.
DRF: token + session auth, `IsAuthenticated` by default (public endpoints opt
out explicitly), JSON-only rendering when `DEBUG=False`.

## API

All endpoints live under `/api/`. Admin site at `/admin/`.

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET  | `/api/health/` | public | `{"status":"ok","service":"zinwe-backend","version":"0.1.0","time":...}` |
| POST | `/api/auth/early-access/` | public, 20/hour/IP | Join the early-access list |
| POST | `/api/auth/register/` | public | `{email, password, full_name, company?, phone?}` -> `{token, user}` (201) |
| POST | `/api/auth/login/` | public | `{email, password}` -> `{token, user}` |
| GET  | `/api/auth/me/` | token | user fields + `active_plan` (or `null`) |
| GET  | `/api/auth/plans/` | public | active plans |
| POST | `/api/auth/plans/activate/` | token | `{"plan": "<code>"}` -> new active `PlanActivation` (201) |
| GET  | `/api/analytics/summary/` | staff | early-access + event counts |
| POST | `/api/devices/enroll/` | public, 60/hour/IP | Agent: `{enrollment_key, machine_id, hostname, os_version?, agent_version?, platform?}` -> `{device_id, device_token, re_enrolled}` (201 new / 200 re-enrolled; 400 `enrollment_key` = refused) |
| POST | `/api/devices/heartbeat/` | `Device <token>` | Agent check-in -> `{device_id, server_time, next_heartbeat_seconds, policy_version}`; 401 = removed |
| GET  | `/api/devices/policy/current/` | `Device <token>` | Agent: rules to enforce -> `{version, usb: {mode}}` |
| POST | `/api/devices/events/upload/` | `Device <token>` | Agent: `{events: [{id, type, occurred_at, severity?, data?}]}` (1..100) -> `{accepted, duplicates}`; idempotent on `id` |
| GET  | `/api/devices/` | token | owner's devices with `is_online` |
| GET  | `/api/devices/enrollment-key/` | token | owner's active key (created on first call) |
| POST | `/api/devices/enrollment-key/rotate/` | token | revoke + issue a new key (enrolled devices keep working) |
| GET  | `/api/devices/policy/` | token | owner's rules -> `{version, usb: {mode}, updated_at}` (created on first call: v1, `allow`) |
| PUT  | `/api/devices/policy/` | token | `{usb: {mode: allow\|read_only\|block}}` -> same shape; version +1 only on a real change |
| GET  | `/api/devices/events/` | token | activity feed, newest first; `?device=<id>&limit=<1..200, default 50>` |

Authenticate with `Authorization: Token <token>`. Unauthenticated requests to
protected endpoints get **401**; authenticated non-staff on staff endpoints get **403**.

### `POST /api/auth/early-access/`

```json
{
  "name": "Asha Verma",
  "email": "asha@acme.in",
  "company": "Acme Traders",
  "phone": "",
  "team_size": "11-25",
  "platforms": ["windows", "linux"],
  "concerns": ["usb", "compliance"]
}
```

- `name` (required, <=120), `email` (required), `company` (required, <=160),
  `phone` (optional, may be `""`, <=32), `team_size` one of `1-10`, `11-25`, `26-50`, `50+`,
  `platforms` subset of `windows|linux|macos`, `concerns` subset of
  `usb|remote|installs|email|compliance` (both may be empty or omitted).
- Input is normalised: whitespace stripped, email lowercased, list values
  lowercased and de-duplicated.

Responses:

- **201** new: `{"id": 1, "email": "asha@acme.in", "status": "pending", "trial_days": 90, "already_registered": false, "message": "Spot reserved."}`
  - after commit: `early_access.registered` event, confirmation email to the registrant, alert email to `ZINWE_TEAM_NOTIFY_EMAIL`.
- **200** duplicate (case-insensitive email): same shape with the existing record,
  `"already_registered": true, "message": "You're already on the list."` No new row, no email; tracks `early_access.duplicate`.
- **400** validation: `{"field": ["message", ...]}`, e.g. `"Enter a valid email address."`,
  `"Pick one option."` (team_size), `"This field is required."`.
- **429** throttled: `{"detail": "Request was throttled. Expected available in N seconds."}`

Stores the client IP (REMOTE_ADDR; first `X-Forwarded-For` hop only when
`ZINWE_TRUST_X_FORWARDED_FOR=True`) and the user agent (truncated to 255).

### Devices (z-devices)

- Device tokens are stored as SHA-256 only and are accepted **only** by device
  endpoints (`DeviceTokenAuthentication` is opt-in per view), so a token taken
  from a PC can't call account APIs.
- `(owner, machine_id)` is unique: an agent reinstall re-uses the row and rotates the token.
- Revoke a PC in admin (**Devices -> Revoke**); its next heartbeat gets 401.
- Online = seen within `ZINWE_DEVICE_OFFLINE_AFTER_SECONDS` (3 x 60s heartbeat).
- Analytics events: `device.enrolled`, `devices.key_rotated`, `policy.updated`. The agent itself lives in `zinwe/window-app/`.
- Owner views use `IsOwner` (logged in **and** not a device), a second lock on top of
  device auth being opt-in; device views use `DeviceTokenAuthentication` + `IsDevice`,
  so an owner token gets 401 there.

#### Policy

- One `Policy` per owner, created lazily (`version=1`, `usb_mode=allow`). Today it holds
  one rule, the USB mode: `allow`, `read_only` (no writes to USB drives) or `block`.
- `PUT /api/devices/policy/` bumps `version` by one **only** when a rule changes; saving
  the same rules again is a no-op (same version, same `updated_at`, no analytics event).
  Bad mode -> 400 `{"usb": {"mode": ["..."]}}`.
- Every heartbeat returns the owner's current `policy_version`. When it differs from the
  agent's cached copy, the agent calls `GET policy/current/`, saves and applies it, so PCs
  pick up a change within one heartbeat (~60s).
- Rules are read-only in Django admin: change them through the API so the version bumps.

#### Events (activity feed)

- The agent queues events locally and uploads them in batches of 1..100:

  ```json
  {"events": [{"id": "0b6c...uuid", "type": "usb.connected",
               "occurred_at": "2026-10-09T10:15:00+05:30", "severity": "info",
               "data": {"device_name": "SanDisk Ultra", "drive_letter": "E:"}}]}
  ```

  - `id`: uuid made by the agent, stored as `client_id`; unique per device, so re-sending
    after a lost response is safe. Already-stored (or repeated) ids come back as
    `duplicates`, not errors: `{"accepted": 1, "duplicates": 1}`.
  - `type` must match `^[a-z_]+(\.[a-z_]+)+$` (max 64 chars). `severity` is
    `info|warning|critical` (default `info`). `data` is a JSON object, <= 4096 bytes
    serialized (default `{}`). `occurred_at` should carry an offset; a naive time is read as UTC.
  - Any invalid event rejects the whole batch with 400; errors are keyed by the failing
    event's index: `{"events": {"1": {"severity": ["..."]}}}`.
- `GET /api/devices/events/` returns the owner's events (revoked PCs included), newest
  `occurred_at` first: `{id, device_id, hostname, type, severity, title, occurred_at,
  received_at, data}`. `?device=` for one PC (someone else's id just returns `[]`),
  `?limit=` 1..200 (default 50; out of range -> 400).
- `title` is built at read time in `z_devices/events.py` from type + data + hostname:

  | type | severity | title |
  |------|----------|-------|
  | `usb.connected` | info | `USB drive "SanDisk Ultra" plugged into ACCOUNTS-PC-01` |
  | `usb.blocked` | warning | `Blocked USB drive "SanDisk Ultra" on ACCOUNTS-PC-01` |
  | `usb.policy_applied` | info | `USB rule "Read-only" is now active on ACCOUNTS-PC-01` (+ ` (test mode)` when `dry_run`) |
  | anything else | as sent | `ACCOUNTS-PC-01 reported a "file copied" event` |

  Values from `data` are untrusted: only strings are used, whitespace is collapsed and
  they're cut at 80 characters.

## Plans

Seeded by migration `z_auth/0002_seed_plans.py`:

| code | name | price (INR/month) | trial_days | active |
|------|------|-------------------|------------|--------|
| `free` | Free | 0 | 0 | yes |
| `early-access` | Early access — full plan, 3 months free | 0 | 90 | yes |
| `pro` | Pro | 0 (TODO: real pricing) | 0 | no |

Activating a plan deactivates the user's previous active plan. Plans with
`trial_days > 0` get `ends_at = now + trial_days`; others are open-ended.

## Notifications

`z_notification.services.notify(channel, recipient, template, context, ref=None)`
renders `z_notification/templates/z_notification/<template>/subject.txt`,
`body.txt` and (optional) `body.html`, writes a `Notification` row, sends it, and
records `sent`/`failed`. It never raises to the caller.

### Adding a channel (SMS, WhatsApp, Slack, ...)

1. Create `z_notification/channels/sms.py`:

   ```python
   from .base import Channel

   class SmsChannel(Channel):
       name = "sms"

       def send(self, recipient, subject, body, html=None):
           provider_client.send(to=recipient, text=body)  # raise on failure
   ```

2. Register it once at startup, e.g. in `z_notification/apps.py`:

   ```python
   def ready(self):
       from .channels import register_channel
       from .channels.sms import SmsChannel
       register_channel(SmsChannel())
   ```

3. Send: `notify("sms", "+919810000000", "early_access_confirmation", context)`.
   Templates are shared across channels; channels that can't show HTML just ignore `html`.

## Analytics

`z_analytics.services.track(name, properties=None, actor_email="", ref=None)`
writes an `Event` row and never raises. Current events:
`early_access.registered`, `early_access.duplicate`, `plan.activated`.

## z-main

Put generic, cross-app endpoints and helpers here (health, config/feature flags,
contact forms, etc.). Shared helpers today: `z_main/refs.py` (loose related-object
references) and `z_main/client.py` (client IP / user agent).
