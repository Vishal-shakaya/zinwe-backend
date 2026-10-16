# Zinwe backend

Django + Django REST framework API for **Zinwe** (SMB data-protection / activity
monitoring, by SKYTECHNO, New Delhi). Pre-launch: it currently powers the
website's early-access waitlist, the owner portal (accounts, company, plans: Free
forever, Startups, Businesses; manual payments), devices, notifications and analytics.

## Apps

Python packages can't contain hyphens, so the app names map like this:

| Name used in planning | Python package   | What it owns |
|-----------------------|------------------|--------------|
| z-auth                | `z_auth`         | `User` (email login), `Plan`, `PlanActivation`, `EarlyAccessRegistration`, `OneTimeCode`, `Company`, `Subscription`, `ManualPayment`; sign-up/OTP/login/me/password reset; company; plans; subscription + manual payments |
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
| `EMAIL_FILE_PATH` | `<project>/sent-emails` (used by the file email backend, e.g. for end-to-end runs that read OTPs) |
| `ZINWE_ASGI_HOST` / `ZINWE_ASGI_PORT` | `127.0.0.1` / `8000` (`manage.py serve`) |
| `ZINWE_ASGI_WORKERS` | `1` (ignored while reload is on) |
| `ZINWE_ASGI_RELOAD` | same as `DJANGO_DEBUG` |
| `ZINWE_ASGI_LOG_LEVEL` | `info` |
| `ZINWE_FORWARDED_ALLOW_IPS` | `127.0.0.1` (proxy IPs trusted for `X-Forwarded-*`) |
| `DJANGO_MEDIA_ROOT` | `<project>/media` (payment proofs; gitignored, never served publicly) |
| `ZINWE_PAYMENT_UPI_ID` / `_PAYEE_NAME` / `_BANK_NAME` / `_ACCOUNT_TYPE` / `_ACCOUNT_NUMBER` / `_IFSC` | `""` (payee name: `SKYTECHNO`); real values live in `.env` only, never in code; shown on the subscribe page |
| `ZINWE_PAYMENT_PAYPAL_URL` | `""`; PayPal link for payments from outside India (an empty value hides PayPal) |
| `ZINWE_PAYMENT_NOTE` | `Use your company name as the payment reference.` |

SQLite (`db.sqlite3`) locally, `TIME_ZONE=Asia/Kolkata`, `USE_TZ=True`.
DRF: token + session auth, `IsAuthenticated` by default (public endpoints opt
out explicitly), JSON-only rendering when `DEBUG=False`.

## API

All endpoints live under `/api/`. Admin site at `/admin/`.

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET  | `/api/health/` | public | `{"status":"ok","service":"zinwe-backend","version":"0.1.0","time":...}` |
| POST | `/api/auth/early-access/` | public, 20/hour/IP | Join the early-access list |
| POST | `/api/auth/signup/` | public, 10/hour/IP | `{full_name, email, password}` -> 201 `{email, otp_sent, resend_in}`; unverified account + emailed code |
| POST | `/api/auth/signup/verify/` | public, otp scope | `{email, code}` -> AuthResponse; 400 `{"code": ["That code is incorrect or has expired."]}` |
| POST | `/api/auth/otp/resend/` | public, otp scope | `{email, purpose: signup\|login\|password_reset}` -> generic 200 `{detail, resend_in}`; 429 cooldown |
| POST | `/api/auth/login/` | public, 20/hour/IP | `{email, password}` -> AuthResponse; 400 wrong; 403 `{"code": "email_not_verified", ...}` (new code sent) |
| POST | `/api/auth/login/otp/request/` | public, otp scope | `{email}` -> generic 200 `{detail, resend_in}` |
| POST | `/api/auth/login/otp/verify/` | public, 20/hour/IP | `{email, code}` -> AuthResponse |
| POST | `/api/auth/logout/` | token | 204, token deleted |
| GET  | `/api/auth/me/` | token | User + `onboarding`, `company`, `subscription`, `active_plan` (each may be `null`) |
| POST | `/api/auth/password/forgot/` | public, otp scope | `{email}` -> generic 200 `{detail, resend_in}` |
| POST | `/api/auth/password/reset/` | public, otp scope | `{email, code, new_password}` -> AuthResponse; **all old tokens revoked** |
| GET/POST/PATCH | `/api/auth/company/` | token | the owner's single company: 404 none / 201 created / 409 already registered / 200 patched |
| GET  | `/api/auth/plans/` | public (token optional) | active plans by `sort_order` (tagline, max_devices, features, is_recommended, requires_payment, `trial_available` for the caller) |
| GET  | `/api/auth/subscription/` | token | `{subscription \| null, instructions}` (subscription has `is_trial`; instructions include `account_type`, `paypal_url`). Every way to pay is returned whatever the country (UPI, bank, Skydo link / US account); `region` only picks the first tab. `payment_link` falls back to `ZINWE_SKYDO_PAYMENT_URL` |
| POST | `/api/auth/subscription/` | token | `{plan}` -> 201; free plan or free trial `active` now, paid `pending_payment` |
| POST | `/api/auth/subscription/payment/` | token, 10/hour/IP | **multipart** `proof` (JPG/PNG/PDF <= 5 MB), `accept_terms`, optional `method, amount_inr, reference, paid_on, payer_name, upgrade_plan`. A receipt alone is enough: the amount due, today and the owner's name are filled in; without a receipt `reference` is required -> 201 `submitted` |
| GET  | `/api/auth/subscription/quote/` | token | the shared quote: number, dates, customer, plan, `quote` (amounts), `instructions`, `public_url` (`ZINWE_APP_URL/quote/<token>`). 409 when nothing is due |
| GET  | `/api/auth/subscription/quote/pdf/` | token | the quote as a PDF (with UPI QR, bank and Skydo details) |
| POST | `/api/auth/subscription/quote/email/` | token, 10/hour | `{email?}` (blank = the owner) -> emails the PDF as an attachment + the public link -> `{sent_to, public_url}` |
| GET  | `/api/auth/quotes/<token>/`, `.../pdf/` | public, 120/hour | the shared quote for the website's `/quote/<token>` page; 404 once nothing is due on that subscription |
| GET  | `/api/auth/subscription/payment/<id>/proof/` | token | download your own proof (staff: any) |
| POST | `/api/auth/register/` | public | **Deprecated** (use `signup/`): `{email, password, full_name, company?, phone?}` -> `{token, user}` (201), account verified at once |
| POST | `/api/auth/plans/activate/` | token | Older endpoint: `{"plan": "<code>"}` -> new active `PlanActivation` (201); refuses paid plans |
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

- **201** new: `{"id": 1, "email": "asha@acme.in", "status": "pending", "trial_days": 0, "already_registered": false, "message": "Spot reserved."}`
  - after commit: `early_access.registered` event, confirmation email to the registrant, alert email to `ZINWE_TEAM_NOTIFY_EMAIL`.
- **200** duplicate (case-insensitive email): same shape with the existing record,
  `"already_registered": true, "message": "You're already on the list."` No new row, no email; tracks `early_access.duplicate`.
- **400** validation: `{"field": ["message", ...]}`, e.g. `"Enter a valid email address."`,
  `"Pick one option."` (team_size), `"This field is required."`.
- **429** throttled: `{"detail": "Request was throttled. Expected available in N seconds.", "retry_after": N}`
  (every 429 in the API carries `retry_after`, via `z_main/exceptions.py`)

Stores the client IP (REMOTE_ADDR; first `X-Forwarded-For` hop only when
`ZINWE_TRUST_X_FORWARDED_FOR=True`) and the user agent (truncated to 255).

### Accounts, onboarding and billing (owner portal)

The full contract (shapes, messages, step rules) is `zinwe/.cloude/system-design-agent/portal-v1.md`.
Code: `z_auth/otp.py` (codes), `services.py` (accounts), `company.py`, `billing.py`,
`onboarding.py`; views in `views.py` (auth) and `portal_views.py` (company/subscription).

- **AuthResponse** = `{token, user, onboarding}`. `onboarding.step` is the first match of:
  `company` (no Company) -> `plan` (no subscription, or cancelled/expired) -> `payment`
  (pending, nothing submitted) -> `payment_review` (pending, a payment submitted) -> `complete` (active).
- **Email codes**: 6 digits, 10 minutes, 5 wrong tries burn the code, 60 s between sends
  (429 + `retry_after`), at most 5 sends per email + purpose per hour. Only an HMAC-SHA256
  (keyed with `SECRET_KEY`) is stored; a new code kills older ones. Code rows are written even for
  unknown addresses, so `otp/resend`, `login/otp/request` and `password/forgot` answer the same way
  (and hit the same cooldown) whether or not the account exists. The `Notification` log stores the
  email body with the code masked as `••••••` (`notify(..., redact=[code])`).
- **Sign-up** creates `is_active=False`, `email_verified_at=null`; the code activates it. Re-submitting
  sign-up for an unverified email updates name/password; inside the 60 s cooldown it returns
  `otp_sent: false` and the earlier code still works.
- **Free trial (dormant since migration 0007: no plan has `trial_days`)**: one per account, ever (`services.trial_used`: any `is_trial` subscription, or an
  activation of the legacy `early-access` plan). Subscribing to a paid plan with `trial_days`
  (Businesses had 90 days until v1.2) while the trial is unused starts it at once: `active`, `is_trial`,
  `amount_inr` 0, ends after `trial_days`, no payment step. After that, the same plan means paying
  (`pending_payment`), and verifying that payment works normally. Starting a trial marks the owner's
  early-access waitlist row (if any) `activated`. `GET plans/` reads the token if one is sent:
  `trial_available` is false once the caller has used their trial (a stale token counts as anonymous).
- **Payment methods**: UPI, bank transfer, PayPal (payments from outside India; the UI opens
  `paypal_url`, the owner reports the PayPal transaction ID), cheque, cash.
- An `active` subscription past `ends_at` is flipped to `expired` (and its PlanActivation switched off)
  the next time it's read, which sends the owner back to the `plan` step.
- Throttle scopes (per IP): `auth_signup` 10/h, `auth_login` 20/h, `auth_otp` 30/h (shared by the
  code endpoints), `payment_submit` 10/h.
- Emails (`z_notification/templates/z_notification/`): `otp_signup`, `otp_login`, `otp_password_reset`,
  `welcome`, `subscription_active`, `payment_received_team` (to `ZINWE_TEAM_NOTIFY_EMAIL`), `payment_rejected`.
- Analytics events: `auth.signup_started`, `auth.signup_verified`, `auth.login` (`method`),
  `auth.password_reset`, `company.registered`, `subscription.created`, `payment.submitted`,
  `payment.verified`, `payment.rejected`.

#### Verifying a manual payment (Django admin)

1. The team gets a "Payment to check" email. Open **/admin/ -> Manual payments** (filter by
   *Status: Submitted*). Open a row and use **Download proof** to see the screenshot/receipt
   (proofs are staff-only; there is no public media URL).
2. Back on the list, tick the payment(s) and pick an action:
   - **Verify payment** -> payment `verified`, subscription `active` from now for one month,
     a PlanActivation for the plan (the owner's only active one), "plan is active" email to the owner.
   - **Reject payment (asks for a reason)** -> a second page asks for the reason (sent to the owner).
     The payment becomes `rejected`, the subscription stays `pending_payment`, so the owner can submit again.
3. Payments that were already reviewed are skipped with a warning.

Prices are flat monthly amounts; change them in **Plans** (subscriptions snapshot `amount_inr`).

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

Seeded by `z_auth/0002_seed_plans.py`, updated by `0005_update_plan_catalogue.py` and
`0006_v1_1_catalogue_trial_paypal.py` (portal v1.1, founder decision 2026-10-09):

| code | name | price (INR/month) | trial_days | max PCs | requires payment | recommended | sort |
|------|------|-------------------|------------|---------|------------------|-------------|------|
| `free` | Free forever | 0 | 0 | 3 | no | no | 10 |
| `startups` | Startups | 971 (list 1,349, 28% off) | 0 | 10 | yes | no | 20 |
| `businesses` | Businesses | 2,834 (list 4,049, 30% off) | 90 (free trial, once per account) | 50 | yes | yes | 30 |

The old `early-access` (free, 90-day trial) and `pro` plans are `is_active=False`; their rows stay
because subscriptions and activations point at them. Feature lines and taglines are seeded and
editable in admin.

Activating a plan deactivates the user's previous active plan. A trial ends after `trial_days`,
a verified payment after one month; the free plan is open-ended.

## Notifications

`z_notification.services.notify(channel, recipient, template, context, ref=None, redact=None)`
renders `z_notification/templates/z_notification/<template>/subject.txt`,
`body.txt` and (optional) `body.html`, writes a `Notification` row, sends it, and
records `sent`/`failed`. It never raises to the caller. Strings in `redact` (e.g. a one-time
code) are sent but replaced with `••••••` in the logged subject, body and error.

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
`early_access.registered`, `early_access.duplicate`, `plan.activated`, plus the account /
billing events listed under *Accounts, onboarding and billing*.

## z-main

Put generic, cross-app endpoints and helpers here (health, config/feature flags,
contact forms, etc.). Shared helpers today: `z_main/refs.py` (loose related-object
references), `z_main/client.py` (client IP / user agent) and `z_main/exceptions.py`
(DRF exception handler adding `retry_after` to 429s).
