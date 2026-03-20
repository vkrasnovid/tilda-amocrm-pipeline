# Implementation Plan: Tilda → AmoCRM + Email-Chain Integration

Branch: feature/tilda-amocrm-pipeline
Created: 2026-03-20

## Settings
- Testing: no
- Logging: verbose
- Docs: no

## Commit Plan
- **Commit 1** (after tasks 1–4): `feat: project scaffold, config, DB schema, migrations`
- **Commit 2** (after tasks 5–7): `feat: webhook endpoint, HMAC validation, lead upsert`
- **Commit 3** (after tasks 8–10): `feat: AmoCRM httpx client, OAuth2 token refresh, celery task, retry logic`
- **Commit 4** (after tasks 11–13): `feat: email chain scheduling and SMTP send tasks`
- **Commit 5** (after tasks 14–16): `feat: IMAP polling, chain stop, Telegram notify`
- **Commit 6** (after tasks 17–19): `feat: admin API, health endpoint, Docker Compose`

---

## Tasks

### Phase 1: Project Scaffold & Configuration

- [x] **Task 1: Repo structure & requirements.txt**
  Create the top-level Python package layout, all subdirectory packages, and dependency file.

  Files to create:
  - `app/__init__.py`
  - `app/config.py`
  - `app/db/__init__.py`
  - `app/integrations/__init__.py`
  - `app/routers/__init__.py`
  - `app/tasks/__init__.py`
  - `requirements.txt`
  - `.env.example`

  Details:
  - `requirements.txt` must pin: `fastapi`, `uvicorn[standard]`, `celery[redis]`, `redis`,
    `aiosqlite`, `sqlalchemy[asyncio]`, `alembic`, `httpx`, `aiosmtplib`, `aioimaplib`,
    `aiogram>=3`, `pydantic-settings`, `jinja2`, `python-multipart`, `slowapi`
  - `app/config.py`: `pydantic-settings` class `Settings` loading all `.env` variables
    from ARCHITECTURE.md §7 (APP_*, TILDA_*, ADMIN_*, DATABASE_*, REDIS_*, CELERY_*,
    AMOCRM_*, SMTP_*, IMAP_*, TELEGRAM_*)
  - `.env.example`: placeholder values for every variable in §7; no real secrets
  - All `__init__.py` files for subdirs (`app/db/`, `app/integrations/`, `app/routers/`,
    `app/tasks/`) must be empty files — they are required for Python to treat these as packages

  LOGGING REQUIREMENTS:
  - `config.py`: `DEBUG` log at startup listing all non-secret setting keys that were loaded
  - Use Python stdlib `logging`; root logger configured from `LOG_LEVEL` env var
  - Format: `[%(name)s] %(levelname)s %(message)s`

---

- [x] **Task 2: SQLAlchemy models (Core) + database session**
  Define the three tables exactly as in ARCHITECTURE.md §4 using SQLAlchemy Core metadata.

  Files to create:
  - `app/db/models.py`      — `metadata`, `leads_table`, `email_events_table`, `imap_poll_log_table`
  - `app/db/session.py`     — async engine factory, `get_db()` context manager

  Details:
  - All columns, types, constraints, and indexes exactly as in §4
  - `created_at` / `updated_at`: server_default = `func.now()`; `updated_at` also has
    `onupdate = func.now()` (SQLAlchemy Core pattern: use `Column(onupdate=...)`)
  - `email_events.lead_id` FK with `CASCADE` delete
  - Engine: `create_async_engine(settings.DATABASE_URL, echo=settings.LOG_LEVEL=="DEBUG")`
  - `get_db()`: returns `AsyncConnection` via `async with engine.begin() as conn`

  LOGGING REQUIREMENTS:
  - `DEBUG`: log when engine is created, including dialect + DB path
  - `DEBUG`: log each `get_db()` connection acquired/released
  - `ERROR`: log full traceback on connection failure

---

- [x] **Task 3: Alembic migrations setup**
  Initialise Alembic and create the initial migration that creates all three tables.

  Files to create:
  - `alembic.ini`
  - `alembic/env.py`
  - `alembic/versions/0001_initial_schema.py`

  Details:
  - `alembic/env.py`: use `run_migrations_online` with asyncio runner (`asyncio.run`) and
    the same `create_async_engine` from `app/db/session.py`
  - Migration creates: `leads`, `email_events`, `imap_poll_log` with all indexes and FK
  - `alembic upgrade head` must be runnable standalone (for Docker entrypoint)

  LOGGING REQUIREMENTS:
  - Alembic already logs to `alembic` logger; set it to `INFO` in `alembic.ini`
  - Add `DEBUG` log in `env.py` before and after migration run: `[alembic.env] running migrations`

---

- [x] **Task 4: Celery app initialisation**
  Create the Celery application instance, queue definitions, and beat schedule.

  Files to create:
  - `app/celery_app.py`

  Details:
  - `Celery("pipeline", broker=settings.CELERY_BROKER_URL, backend=settings.CELERY_RESULT_BACKEND)`
  - Configure four queues: `default`, `amocrm`, `email`, `telegram` (see ARCHITECTURE.md Appendix)
  - Beat schedule: `imap_poll_inbox` every `settings.IMAP_POLL_INTERVAL_SECONDS` seconds
    on the `default` queue
  - Task serialisation: `json`; `task_track_started = True`
  - Use `app.autodiscover_tasks(['app.tasks'])` for task discovery — this avoids circular import
    issues since task modules are created in later phases. Add a comment:
    `# Task modules created in Phases 3-5 are auto-discovered via this call`
  - Do NOT manually import individual task modules at this point

  LOGGING REQUIREMENTS:
  - `INFO`: log Celery app startup with broker URL (mask password: replace `:<password>@` with `:***@`)
  - `DEBUG`: log beat schedule entries at startup

---

### Phase 2: Webhook Endpoint (Story 1)

- [x] **Task 5: Pydantic schemas for Tilda webhook**
  Define request/response models for the webhook.

  Files to create:
  - `app/schemas.py`

  Details:
  - `TildaWebhookPayload`: fields `name: str`, `email: EmailStr`, `phone: str | None`,
    `formid: str | None` — handles both JSON and form-data
  - `WebhookResponse`: `status: str`, `lead_id: int`
  - `ErrorResponse`: `status: str = "error"`, `detail: str`
  - `LeadOut`: all columns from `leads` table for admin API
  - `LeadDetailOut`: `LeadOut` + `email_events: list[EmailEventOut]`
  - `EmailEventOut`: all columns from `email_events`

  LOGGING REQUIREMENTS:
  - No runtime logging needed in schemas; validation errors bubble up to FastAPI handler

---

- [x] **Task 6: HMAC-SHA256 webhook signature validation**
  Implement the security dependency that validates `X-Tilda-Signature`.

  Files to create:
  - `app/security.py`

  Details:
  - FastAPI `Depends`-compatible function `verify_tilda_signature(request: Request)`
  - Reads raw body bytes, computes `hmac.new(TILDA_WEBHOOK_SECRET.encode(), body, sha256).hexdigest()`
  - Compares with `X-Tilda-Signature` header using `hmac.compare_digest`
  - Raises `HTTPException(401)` on mismatch or missing header
  - Also: `verify_admin_token(credentials: HTTPAuthorizationCredentials)` using `HTTPBearer`
    comparing with `ADMIN_API_TOKEN` via `secrets.compare_digest`

  LOGGING REQUIREMENTS:
  - `DEBUG`: log `[security] HMAC check: header=<first8chars>... computed=<first8chars>...`
  - `WARNING`: log `[security] Invalid HMAC signature from {request.client.host}`
  - `WARNING`: log `[security] Missing X-Tilda-Signature from {request.client.host}`

---

- [x] **Task 7: POST /webhook/tilda endpoint + lead upsert**
  Implement the main webhook handler with lead persistence and task enqueueing.

  Files to create:
  - `app/routers/webhook.py`
  - `app/main.py`

  Details:
  - `POST /webhook/tilda`: depends on `verify_tilda_signature`
  - Accept both `application/json` and `application/x-www-form-urlencoded` (use `Request`
    body parsing with content-type check, or `Form` fields; JSON preferred)
  - Lead upsert logic (in a helper `upsert_lead(conn, payload) -> int`):
    ```sql
    INSERT INTO leads (email, name, phone, source_form_id, created_at, updated_at)
    VALUES (?, ?, ?, ?, now(), now())
    ON CONFLICT(email) DO UPDATE SET
      name=excluded.name, phone=excluded.phone,
      chain_status='active', updated_at=now()
    RETURNING id
    ```
  - After upsert: enqueue `create_amocrm_deal.apply_async([lead_id], queue='amocrm')`
    and `schedule_email_chain.apply_async([lead_id], queue='default')`
  - Return `{"status": "ok", "lead_id": lead_id}`
  - Rate limit: `slowapi` limiter on `/webhook/tilda`: 60/minute per IP
  - `main.py`: create `FastAPI` app, include only the webhook router here (health/admin
    routers are added in Task 17), add `SlowAPIMiddleware`
  - Lifespan startup uses FastAPI 0.93+ `@asynccontextmanager` pattern:
    ```python
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import subprocess
        subprocess.run(["alembic", "upgrade", "head"], check=True)
        yield
    app = FastAPI(lifespan=lifespan)
    ```

  LOGGING REQUIREMENTS:
  - `INFO`: `[webhook] Received lead: email={email} name={name} form={formid}`
  - `INFO`: `[webhook] Lead upserted: lead_id={id} (new={bool})`
  - `DEBUG`: `[webhook] Enqueued tasks: amocrm_task_id={} email_chain_task_id={}`
  - `WARNING`: `[webhook] Duplicate lead re-activated: email={email}`
  - `ERROR`: log DB errors with full context before raising 500

<!-- Commit 2 checkpoint: tasks 5–7 -->

---

### Phase 3: AmoCRM Integration (Story 2)

- [x] **Task 8: AmoCRM HTTP client**
  Async httpx client for AmoCRM REST API with auth and retry logic.

  Files to create:
  - `app/integrations/amocrm.py`

  Details:
  - Class `AmoCRMClient` with `AsyncClient` (base_url, Authorization Bearer header)
  - Methods:
    - `find_contact_by_email(email: str) -> int | None` — GET `/api/v4/contacts?query=<email>`
    - `create_contact(name, email, phone) -> int` — POST `/api/v4/contacts`
    - `create_deal(name, pipeline_id, stage_id) -> int` — POST `/api/v4/leads`
    - `link_contact_to_deal(deal_id, contact_id)` — POST `/api/v4/leads/{deal_id}/links`
  - All methods: exponential backoff retry (tenacity or manual) — max 3 retries,
    delays 60s/120s/240s; retry on 5xx and network errors
  - Parse response JSON carefully; log raw response at DEBUG

  LOGGING REQUIREMENTS:
  - `DEBUG`: `[amocrm] → {method} {url} payload={payload_summary}`
  - `DEBUG`: `[amocrm] ← {status_code} {response_body_summary}`
  - `INFO`: `[amocrm] Contact found/created: contact_id={}`
  - `INFO`: `[amocrm] Deal created: deal_id={}`
  - `WARNING`: `[amocrm] Retry {n}/3 after error: {error}`
  - `ERROR`: `[amocrm] Final failure after 3 retries: {error}`

---

- [x] **Task 8a: AmoCRM OAuth2 token refresh**
  Implement automatic OAuth2 access token refresh for AmoCRM (tokens expire after 24h).

  Add to:
  - `app/integrations/amocrm.py`

  Files to create:
  - `app/integrations/token_store.py` — reads/writes tokens to a JSON file on the volume

  Details:
  - `token_store.py`:
    - `TOKEN_FILE = Path(os.environ.get("TOKEN_FILE", "/data/amocrm_tokens.json"))`
    - `load_tokens() -> dict`: reads `{access_token, refresh_token}` from JSON file;
      falls back to `settings.AMOCRM_ACCESS_TOKEN` / `settings.AMOCRM_REFRESH_TOKEN` if file absent
    - `save_tokens(access_token, refresh_token)`: writes atomically to `TOKEN_FILE`
      (write to `.tmp` then `os.replace`)
  - `AmoCRMClient`: add `_refresh_access_token()` method:
    - POST `{AMOCRM_BASE_URL}/oauth2/access_token` with `grant_type=refresh_token`,
      `client_id`, `client_secret`, `refresh_token`, `redirect_uri`
    - On success: save new tokens via `token_store.save_tokens()`; update `Authorization` header
    - On failure: raise; log ERROR with `[amocrm] Token refresh failed`
  - Wrap all API calls: on `401` response, attempt one token refresh then retry the original request
  - Token file must NOT be committed (add `/data/amocrm_tokens.json` to `.dockerignore`)

  LOGGING REQUIREMENTS:
  - `INFO`: `[amocrm] Access token refreshed, new expiry stored to {TOKEN_FILE}`
  - `WARNING`: `[amocrm] Access token expired, attempting refresh`
  - `ERROR`: `[amocrm] OAuth2 token refresh failed: {error}`

---

- [x] **Task 9: create_amocrm_deal Celery task**
  Celery task that creates contact + deal in AmoCRM and updates the lead record.

  Files to create:
  - `app/tasks/amocrm.py`

  Details:
  - `@celery_app.task(bind=True, queue='amocrm', max_retries=3, name='create_amocrm_deal')`
  - Task body (synchronous wrapper running async code via `asyncio.run()`):
    1. `SELECT leads WHERE id=lead_id` — get name, email, phone
    2. `AmoCRMClient.find_contact_by_email(email)` → contact_id or None
    3. If None: `create_contact(name, email, phone)` → contact_id
    4. `create_deal(name, pipeline_id, stage_id)` → deal_id
    5. `link_contact_to_deal(deal_id, contact_id)`
    6. `UPDATE leads SET amocrm_contact_id, amocrm_deal_id, amocrm_status='created'`
  - On exception: `self.retry(exc=exc, countdown=60 * 2**self.request.retries)`
  - After max retries: `UPDATE leads SET amocrm_status='failed'`

  LOGGING REQUIREMENTS:
  - `INFO`: `[task.amocrm] Starting create_amocrm_deal: lead_id={}`
  - `INFO`: `[task.amocrm] AmoCRM deal created: lead_id={} deal_id={} contact_id={}`
  - `WARNING`: `[task.amocrm] Retry {n} for lead_id={}: {error}`
  - `ERROR`: `[task.amocrm] Failed after all retries: lead_id={} error={}`

---

- [x] **Task 10: schedule_email_chain Celery task**
  Task that inserts email_events rows and schedules the three send_email tasks.

  Files to create:
  - `app/tasks/email_chain.py` (initial file with `schedule_email_chain` only)

  Details:
  - `@celery_app.task(queue='default', name='schedule_email_chain')`
  - For steps 1/2/3, insert `email_events` rows:
    - step 1: `scheduled_at = now`
    - step 2: `scheduled_at = now + 48h`
    - step 3: `scheduled_at = now + 120h`
  - Use `INSERT OR IGNORE` (unique constraint on `lead_id, step`) for idempotency
  - Schedule each `send_email` task: `countdown=0`, `172800`, `432000` seconds
  - Store returned `celery_task_id` (from `AsyncResult.id`) in `email_events.celery_task_id`

  LOGGING REQUIREMENTS:
  - `INFO`: `[task.email_chain] Scheduling 3 emails for lead_id={}`
  - `DEBUG`: `[task.email_chain] Inserted email_event: step={} scheduled_at={} task_id={}`
  - `WARNING`: `[task.email_chain] Duplicate schedule skipped for lead_id={} step={}`

<!-- Commit 3 checkpoint: tasks 8–10 -->

---

### Phase 4: Email Sending (Story 3)

- [x] **Task 11: Jinja2 HTML email templates**
  Create the three HTML email templates.

  Files to create:
  - `templates/email_step_1.html`
  - `templates/email_step_2.html`
  - `templates/email_step_3.html`

  Details:
  - Step 1: "Приветствие" — welcome message using `{{ name }}` and `{{ email }}`
  - Step 2: "Напоминание" — follow-up reminder
  - Step 3: "Спецпредложение" — special offer
  - All templates: valid HTML5 with `<html>`, `<head>`, `<body>`; inline styles
  - Include `List-Unsubscribe` header note in a comment (actual header set in SMTP task)
  - Jinja2 autoescaping enabled (pass `autoescape=True` when creating `Environment`)

  LOGGING REQUIREMENTS: N/A (static files)

---

- [x] **Task 12: SMTP email client**
  Async aiosmtplib helper for sending HTML emails.

  Files to create:
  - `app/integrations/smtp.py`

  Details:
  - Function `send_html_email(to_email, to_name, subject, html_body) -> None`
  - Build `email.mime.multipart.MIMEMultipart('alternative')` message
  - Set headers: `From`, `To`, `Subject`, `List-Unsubscribe: <mailto:unsubscribe@...>`
  - Attach HTML part as `MIMEText(html_body, 'html', 'utf-8')`
  - Connect via `aiosmtplib.send()` or manual `SMTP` context manager
  - Use `STARTTLS` when `SMTP_USE_TLS=true` (port 587); SSL when false (port 465)

  LOGGING REQUIREMENTS:
  - `DEBUG`: `[smtp] Connecting to {host}:{port} tls={use_tls}`
  - `INFO`: `[smtp] Email sent to {to_email} subject="{subject}"`
  - `ERROR`: `[smtp] Send failed to {to_email}: {error}`

---

- [x] **Task 13: send_email Celery task**
  Task that fetches lead, renders template, sends email, updates event status.

  Add to:
  - `app/tasks/email_chain.py`

  Details:
  - `@celery_app.task(bind=True, queue='email', max_retries=3, name='send_email')`
  - Steps:
    1. `SELECT leads WHERE id=lead_id AND chain_status='active'` — skip if not active
    2. `SELECT email_events WHERE lead_id=? AND step=?` — check status != 'cancelled'
    3. Load and render `templates/email_step_{step}.html` via Jinja2 with `{name, email}`
    4. Send via `smtp.send_html_email()`
    5. `UPDATE email_events SET status='sent', sent_at=now()`
  - On SMTP error: `self.retry(countdown=30 * 3**self.request.retries)` (30s/90s/270s)
  - After max retries: `UPDATE email_events SET status='failed', error_message=str(exc), retry_count=max_retries`

  LOGGING REQUIREMENTS:
  - `INFO`: `[task.send_email] Starting: lead_id={} step={}`
  - `INFO`: `[task.send_email] Sent step={} to email={}`
  - `DEBUG`: `[task.send_email] Chain not active, skipping: lead_id={} chain_status={}`
  - `WARNING`: `[task.send_email] Retry {n}/3 for lead_id={} step={}: {error}`
  - `ERROR`: `[task.send_email] Failed after all retries: lead_id={} step={} error={}`

<!-- Commit 4 checkpoint: tasks 11–13 -->

---

### Phase 5: IMAP Polling & Stop Chain (Story 4) + Telegram (Story 5)

- [x] **Task 14: IMAP client**
  Async aioimaplib helper to poll INBOX for unseen messages.

  Files to create:
  - `app/integrations/imap.py`

  Details:
  - Async context manager `IMAPClient` wrapping `aioimaplib.IMAP4_SSL`
  - Method `fetch_unseen_senders() -> list[str]`: LOGIN → `SELECT settings.IMAP_MAILBOX`
    → SEARCH UNSEEN → for each UID: FETCH headers → parse `From:` → extract email address
  - Use `settings.IMAP_MAILBOX` (default `"INBOX"`) — do NOT hardcode the mailbox name
  - Method `mark_seen(uids: list[str])`: STORE `+FLAGS \Seen` for the given UIDs
  - Connection via `settings.IMAP_HOST`, port `settings.IMAP_PORT` (SSL)

  LOGGING REQUIREMENTS:
  - `DEBUG`: `[imap] Connecting to {host}:{port}`
  - `DEBUG`: `[imap] SEARCH UNSEEN returned {n} message(s)`
  - `DEBUG`: `[imap] Parsed sender: {email} from UID {uid}`
  - `ERROR`: `[imap] Connection/fetch error: {error}`

---

- [x] **Task 15: imap_poll_inbox Celery task + chain stop logic**
  Periodic task that polls IMAP, matches leads, stops chains, cancels pending emails.

  Files to create:
  - `app/tasks/imap.py`

  Details:
  - `@celery_app.task(bind=True, queue='default', max_retries=1, name='imap_poll_inbox')`
  - Steps (per ARCHITECTURE.md §6.3):
    1. `IMAPClient.fetch_unseen_senders()` — list of sender emails
    2. For each sender email:
       - `SELECT leads WHERE email=? AND chain_status='active'`
       - If match:
         a. `UPDATE leads SET chain_status='stopped', chain_stopped_at=now(), chain_stop_reason='reply'`
         b. `SELECT email_events WHERE lead_id=? AND status='pending'`
         c. For each: `celery_app.control.revoke(celery_task_id, terminate=False)`
         d. `UPDATE email_events SET status='cancelled'` (bulk update)
         e. `notify_telegram.apply_async([lead_id], queue='telegram')`
    3. `IMAPClient.mark_seen(all_processed_uids)`
    4. `INSERT imap_poll_log (polled_at, messages_checked, matches_found, error=None)`
  - On exception: log error, `INSERT imap_poll_log (..., error=str(exc))`, retry once after 60s

  LOGGING REQUIREMENTS:
  - `INFO`: `[task.imap] Poll started at {timestamp}`
  - `INFO`: `[task.imap] Checked {n} messages, found {m} matches`
  - `INFO`: `[task.imap] Chain stopped for lead_id={} email={}`
  - `DEBUG`: `[task.imap] Revoked celery task {task_id} for email_event step={}`
  - `WARNING`: `[task.imap] Poll error: {error}` (before retry)

---

- [x] **Task 16: Telegram notification client + notify_telegram task**
  aiogram bot wrapper and the Celery fire-and-forget notification task.

  Files to create:
  - `app/integrations/telegram.py`
  - `app/tasks/telegram.py`

  Details:
  - `telegram.py`: async `send_telegram_message(text: str)` using `aiogram.Bot`
    with `settings.TELEGRAM_BOT_TOKEN`; sends to `settings.TELEGRAM_MANAGER_CHAT_ID`
  - `notify_telegram` task: no retries (`max_retries=0`)
    1. `SELECT leads WHERE id=lead_id`
    2. `send_telegram_message(f"Клиент {name} ({email}) ответил на письмо. Цепочка остановлена.")`
    3. On any exception: log WARNING, do not raise

  LOGGING REQUIREMENTS:
  - `INFO`: `[task.telegram] Sending notification for lead_id={} email={}`
  - `INFO`: `[task.telegram] Notification sent to chat_id={}`
  - `WARNING`: `[task.telegram] Notification failed (no retry): lead_id={} error={}`

<!-- Commit 5 checkpoint: tasks 14–16 -->

---

### Phase 6: Admin API, Health, Docker (Story 6)

- [x] **Task 17: GET /health and admin API endpoints**
  Implement the read-only API endpoints and wire all routers into `main.py`.

  Files to create:
  - `app/routers/health.py`
  - `app/routers/admin.py`

  Files to update:
  - `app/main.py` — include health and admin routers:
    ```python
    from app.routers import health, admin
    app.include_router(health.router)
    app.include_router(admin.router, prefix="/admin")
    ```

  Details:
  - `GET /health`:
    - Check DB: use `get_db()` from `app/db/session.py` and execute `SELECT 1`;
      catch exception → `"error"` (do NOT open a separate aiosqlite connection)
    - Check Redis: `redis.asyncio.from_url(settings.REDIS_URL).ping()`, catch → `"error"`
    - Return `{"status": "ok", "db": "ok|error", "redis": "ok|error", "version": "1.0.0"}`
  - `GET /admin/leads`: depends on `verify_admin_token`
    - Query `leads` table with optional filters `chain_status`, `amocrm_status`
    - Pagination: `limit` (default 50, max 200), `offset` (default 0)
    - Rate limit: 30/minute per token (slowapi)
  - `GET /admin/leads/{id}`: depends on `verify_admin_token`
    - JOIN `leads` + `email_events` for the given id; 404 if not found

  LOGGING REQUIREMENTS:
  - `DEBUG`: `[health] DB check: {result}, Redis check: {result}`
  - `DEBUG`: `[admin] GET /leads query: chain_status={} amocrm_status={} limit={} offset={}`
  - `INFO`: `[admin] GET /leads/{id} not found` (before 404)

---

- [x] **Task 18: Dockerfile (multi-stage) + .dockerignore**
  Production-ready multi-stage Docker build exactly as in ARCHITECTURE.md §9.

  Files to create:
  - `Dockerfile`
  - `.dockerignore`

  Details:
  - Stage 1 `builder`: `FROM python:3.11-slim`, copy `requirements.txt`,
    `pip install --no-cache-dir --prefix=/install -r requirements.txt`
  - Stage 2 `runtime`: copy `/install` → `/usr/local`, copy `./app /app/app`,
    copy `./templates /app/templates`, copy `./alembic /app/alembic`, copy `alembic.ini`
  - `WORKDIR /app`; `adduser --disabled-password --no-create-home appuser`; `USER appuser`
  - No `CMD` (overridden per service in compose)
  - `.dockerignore`: `.git`, `__pycache__`, `*.pyc`, `.env`, `*.sqlite3`, `.ai-factory`

  LOGGING REQUIREMENTS: N/A

---

- [x] **Task 19: docker-compose.yml + .env.example**
  Full Docker Compose definition for all four services.

  Files to create:
  - `docker-compose.yml`
  - `.env.example` (update/finalise from Task 1)

  Details:
  - Services: `redis`, `app`, `worker`, `beat` exactly as in ARCHITECTURE.md §9
  - `redis`: `redis:7-alpine`, volume `redis-data:/data`, healthcheck `redis-cli ping`
  - `app`: build `.`, port `8000:8000`, volume `sqlite-data:/data`, env_file `.env`,
    entrypoint: `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1`,
    healthcheck `curl -f http://localhost:8000/health`
  - `worker`: build `.`, volume `sqlite-data:/data`, env_file `.env`, depends on `redis` (healthy) + `app` (healthy),
    entrypoint: `celery -A app.celery_app worker --loglevel=info --concurrency=4 --queues=default,email,amocrm,telegram`
    stop_grace_period: `60s`
  - `beat`: build `.`, volume `sqlite-data:/data`, env_file `.env`, depends on same,
    entrypoint: `celery -A app.celery_app beat --loglevel=info --scheduler=celery.beat:PersistentScheduler --schedule=/data/celerybeat-schedule`
  - Network: `pipeline-net` (bridge), all services attached
  - Volumes: `sqlite-data`, `redis-data`

  LOGGING REQUIREMENTS: N/A

<!-- Commit 6 checkpoint: tasks 17–19 -->
