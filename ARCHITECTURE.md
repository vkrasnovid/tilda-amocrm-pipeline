# Architecture: Tilda → AmoCRM + Email Chain Integration

## 1. System Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              EXTERNAL SYSTEMS                               │
│                                                                             │
│   ┌──────────┐      ┌──────────────┐      ┌──────────┐   ┌──────────────┐  │
│   │  Tilda   │      │   AmoCRM     │      │   SMTP   │   │  Telegram    │  │
│   │  Forms   │      │   REST API   │      │  Server  │   │  Bot API     │  │
│   └────┬─────┘      └──────▲───────┘      └────▲─────┘   └──────▲───────┘  │
└────────┼───────────────────┼───────────────────┼────────────────┼──────────┘
         │ POST /webhook/tilda│                   │                │
         ▼                   │                   │                │
┌─────────────────────────────────────────────────────────────────────────────┐
│                              DOCKER NETWORK                                 │
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │                        app  (FastAPI)                                │   │
│  │                                                                      │   │
│  │   POST /webhook/tilda ──► validate ──► upsert lead ──► enqueue task │   │
│  │   GET  /health                                                       │   │
│  │   GET  /admin/leads                                                  │   │
│  │   GET  /admin/leads/{id}                                             │   │
│  └──────────────────────────────┬───────────────────────────────────────┘   │
│                                 │ Celery tasks via Redis                    │
│                    ┌────────────▼────────────┐                              │
│                    │        redis            │                              │
│                    │  (broker + result store)│                              │
│                    └────────────┬────────────┘                              │
│                                 │                                           │
│            ┌────────────────────┼────────────────────┐                     │
│            │                   │                    │                     │
│  ┌─────────▼──────────┐  ┌─────▼──────────┐  ┌──────▼──────────────────┐  │
│  │  worker (Celery)   │  │  beat (Celery) │  │  worker (Celery)        │  │
│  │                    │  │                │  │  [IMAP polling task]    │  │
│  │  - create_amocrm_  │  │  - schedule    │  │                         │  │
│  │    deal            │  │    imap_poll   │  │  - imap_poll_inbox      │  │
│  │  - send_email_1    │  │    every 5 min │  │    ► match leads        │  │
│  │  - send_email_2    │  │                │  │    ► stop chain         │  │
│  │  - send_email_3    │  └────────────────┘  │    ► notify_telegram    │  │
│  └─────────┬──────────┘                      └─────────────────────────┘   │
│            │                                                                │
│            │  read/write                                                    │
│            ▼                                                                │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │                     SQLite  (aiosqlite)                              │   │
│  │              /data/db.sqlite3  (Docker volume)                       │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘

IMAP Server ──► worker polls every 5 min ──► stop chain ──► Telegram notify
```

---

## 2. Components Table

| Component       | Technology                        | Purpose                                                       |
|-----------------|-----------------------------------|---------------------------------------------------------------|
| **app**         | Python 3.11, FastAPI              | HTTP server: receives Tilda webhook, exposes health + admin   |
| **worker**      | Celery 5.x                        | Executes async tasks: AmoCRM integration, email sending, IMAP |
| **beat**        | Celery Beat                       | Periodic scheduler: triggers IMAP polling every 5 minutes     |
| **redis**       | Redis 7                           | Celery message broker + result backend                        |
| **db**          | SQLite via aiosqlite              | Persistent storage for leads, email events, chain state       |
| **AmoCRM SDK**  | httpx (async HTTP client)         | REST calls to AmoCRM API (contacts, deals)                    |
| **SMTP client** | aiosmtplib                        | Sends HTML email chain messages                               |
| **IMAP client** | aioimaplib                        | Polls inbox for replies; matches by sender email              |
| **Telegram bot**| aiogram 3.x                       | Sends manager notification when client replies                |

---

## 3. Technology Stack

| Layer             | Choice                  | Notes                                              |
|-------------------|-------------------------|----------------------------------------------------|
| Language          | Python 3.11             | Async-first via asyncio                            |
| Web framework     | FastAPI                 | Auto OpenAPI docs, Pydantic validation             |
| Task queue broker | Redis 7 (Alpine)        | Single service for broker + result backend         |
| Task workers      | Celery 5 + Celery Beat  | Worker pool + periodic scheduler                   |
| Database          | SQLite + aiosqlite      | File-based, zero-ops; async driver                 |
| ORM / migrations  | SQLAlchemy 2 (Core)     | Schema definition; Alembic for migrations          |
| Email send        | aiosmtplib              | Async SMTP with TLS/STARTTLS                       |
| Email receive     | aioimaplib              | Async IMAP4 polling                                |
| HTTP client       | httpx (AsyncClient)     | AmoCRM REST API calls with retry logic             |
| Telegram          | aiogram 3.x             | Bot API wrapper; fire-and-forget notifications     |
| Containerisation  | Docker + Docker Compose | Multi-service orchestration                        |
| Config            | pydantic-settings       | Typed .env loading with validation                 |

---

## 4. Database Schema

### Table: `leads`

| Column               | Type          | Constraints                  | Description                               |
|----------------------|---------------|------------------------------|-------------------------------------------|
| `id`                 | INTEGER       | PK, AUTOINCREMENT            | Internal lead ID                          |
| `email`              | TEXT          | NOT NULL, UNIQUE             | Client email — dedup key                  |
| `name`               | TEXT          | NOT NULL                     | Client name from Tilda form               |
| `phone`              | TEXT          |                              | Client phone (optional)                   |
| `source_form_id`     | TEXT          |                              | Tilda form ID (from webhook payload)      |
| `amocrm_contact_id`  | INTEGER       |                              | AmoCRM contact ID after creation          |
| `amocrm_deal_id`     | INTEGER       |                              | AmoCRM deal ID after creation             |
| `amocrm_status`      | TEXT          | DEFAULT 'pending'            | pending / created / failed                |
| `chain_status`       | TEXT          | DEFAULT 'active'             | active / stopped / completed              |
| `chain_stopped_at`   | DATETIME      |                              | UTC timestamp when chain was stopped      |
| `chain_stop_reason`  | TEXT          |                              | reply / manual / completed                |
| `created_at`         | DATETIME      | NOT NULL, DEFAULT now()      | Lead creation timestamp (UTC)             |
| `updated_at`         | DATETIME      | NOT NULL, DEFAULT now()      | Last update timestamp (UTC)               |

**Indexes:**
- `UNIQUE INDEX idx_leads_email ON leads(email)`
- `INDEX idx_leads_chain_status ON leads(chain_status)`
- `INDEX idx_leads_amocrm_deal_id ON leads(amocrm_deal_id)`

---

### Table: `email_events`

| Column           | Type     | Constraints                     | Description                                          |
|------------------|----------|---------------------------------|------------------------------------------------------|
| `id`             | INTEGER  | PK, AUTOINCREMENT               | Internal event ID                                    |
| `lead_id`        | INTEGER  | FK → leads.id, NOT NULL         | Parent lead                                          |
| `step`           | INTEGER  | NOT NULL                        | Email step number: 1, 2, or 3                        |
| `celery_task_id` | TEXT     |                                 | Celery AsyncResult ID for revocation                 |
| `status`         | TEXT     | NOT NULL, DEFAULT 'pending'     | pending / sent / failed / cancelled                  |
| `scheduled_at`   | DATETIME | NOT NULL                        | When this email was/is scheduled to be sent (UTC)    |
| `sent_at`        | DATETIME |                                 | Actual send timestamp (UTC)                          |
| `error_message`  | TEXT     |                                 | Last error string if status = failed                 |
| `retry_count`    | INTEGER  | DEFAULT 0                       | Number of SMTP retry attempts                        |
| `created_at`     | DATETIME | NOT NULL, DEFAULT now()         | Row creation timestamp                               |

**Indexes:**
- `INDEX idx_email_events_lead_id ON email_events(lead_id)`
- `INDEX idx_email_events_status ON email_events(status)`
- `UNIQUE INDEX idx_email_events_lead_step ON email_events(lead_id, step)`

**Foreign keys:**
- `email_events.lead_id → leads.id ON DELETE CASCADE`

---

### Table: `imap_poll_log`

| Column              | Type     | Constraints | Description                                  |
|---------------------|----------|-------------|----------------------------------------------|
| `id`                | INTEGER  | PK, AUTOINCREMENT | Internal ID                            |
| `polled_at`         | DATETIME | NOT NULL    | Timestamp of the poll run (UTC)              |
| `messages_checked`  | INTEGER  | NOT NULL    | Number of inbox messages inspected           |
| `matches_found`     | INTEGER  | NOT NULL    | Number of leads matched and stopped          |
| `error`             | TEXT     |             | Error message if poll run failed             |

---

## 5. API Endpoints

### Webhook

| Method | Path              | Auth              | Description                                                      |
|--------|-------------------|-------------------|------------------------------------------------------------------|
| POST   | `/webhook/tilda`  | HMAC-SHA256 header| Receives Tilda form submission; upserts lead; enqueues tasks     |

**Request body** (JSON or `application/x-www-form-urlencoded`):
```json
{
  "name": "Иван Иванов",
  "email": "ivan@example.com",
  "phone": "+79001234567",
  "formid": "tilda-form-123"
}
```

**Response 200:**
```json
{ "status": "ok", "lead_id": 42 }
```

**Response 400** — validation error (missing email / name):
```json
{ "status": "error", "detail": "email is required" }
```

**Response 401** — invalid HMAC signature.

---

### Health

| Method | Path      | Auth | Description                                         |
|--------|-----------|------|-----------------------------------------------------|
| GET    | `/health` | None | Returns service health: DB connectivity, Redis ping |

**Response 200:**
```json
{
  "status": "ok",
  "db": "ok",
  "redis": "ok",
  "version": "1.0.0"
}
```

---

### Admin (read-only, token-protected)

| Method | Path                  | Auth         | Description                          |
|--------|-----------------------|--------------|--------------------------------------|
| GET    | `/admin/leads`        | Bearer token | List leads with pagination + filters |
| GET    | `/admin/leads/{id}`   | Bearer token | Get single lead with email events    |

**Query params for `/admin/leads`:**
- `chain_status` — filter by chain status
- `amocrm_status` — filter by AmoCRM status
- `limit` (default 50, max 200)
- `offset` (default 0)

---

## 6. Data Flow

### 6.1 Lead ingestion (Tilda → webhook → DB → AmoCRM → email chain)

```
1. Tilda POSTs form data to POST /webhook/tilda
   ├── FastAPI validates HMAC-SHA256 signature (X-Tilda-Signature header)
   ├── Pydantic model validates: email required, name required
   ├── aiosqlite: INSERT OR REPLACE INTO leads (email as dedup key)
   │     └── if email exists → UPDATE name, phone, updated_at; reset chain_status='active'
   └── Return HTTP 200 { "status": "ok", "lead_id": <id> }

2. Celery task: create_amocrm_deal(lead_id)  [enqueued immediately]
   ├── httpx GET /api/v4/contacts?query=<email>
   │     ├── Found → use existing contact_id
   │     └── Not found → POST /api/v4/contacts  →  get contact_id
   ├── POST /api/v4/leads  (pipeline_id from .env, stage "Первичный контакт")
   ├── POST /api/v4/leads/{deal_id}/links  (link contact to deal)
   ├── UPDATE leads SET amocrm_contact_id, amocrm_deal_id, amocrm_status='created'
   └── On error: exponential backoff retry × 3; on final failure: amocrm_status='failed'

3. Celery task: schedule_email_chain(lead_id)  [enqueued after DB save]
   ├── INSERT email_events (lead_id, step=1, status='pending', scheduled_at=now)
   ├── INSERT email_events (lead_id, step=2, status='pending', scheduled_at=now+48h)
   ├── INSERT email_events (lead_id, step=3, status='pending', scheduled_at=now+120h)
   ├── send_email.apply_async(args=[lead_id, 1], countdown=0,     task_id=<uuid>)
   ├── send_email.apply_async(args=[lead_id, 2], countdown=172800, task_id=<uuid>)
   └── send_email.apply_async(args=[lead_id, 3], countdown=432000, task_id=<uuid>)
       → celery_task_id stored in email_events for future revocation
```

### 6.2 Email sending

```
4. Celery task: send_email(lead_id, step)
   ├── SELECT leads WHERE id=lead_id AND chain_status='active'
   │     └── chain_status != 'active' → skip silently (idempotency guard)
   ├── Load HTML template: templates/email_step_{step}.html
   ├── Render template with { name, email } context (Jinja2, autoescaping on)
   ├── aiosmtplib: connect SMTP → STARTTLS/SSL → EHLO → AUTH → SENDMAIL
   ├── UPDATE email_events SET status='sent', sent_at=now()
   └── On SMTP error: retry × 3 with backoff; status='failed' + error_message
```

### 6.3 IMAP monitoring → stop chain → Telegram notify

```
5. Celery Beat: triggers imap_poll_inbox every 5 minutes

6. Celery task: imap_poll_inbox()
   ├── aioimaplib: LOGIN → SELECT INBOX → SEARCH UNSEEN
   ├── For each unseen message:
   │     ├── Parse From: header → extract sender email
   │     ├── SELECT leads WHERE email=<sender> AND chain_status='active'
   │     └── Match found:
   │           ├── UPDATE leads SET chain_status='stopped',
   │           │         chain_stopped_at=now(), chain_stop_reason='reply'
   │           ├── SELECT email_events WHERE lead_id=? AND status='pending'
   │           ├── celery.control.revoke(celery_task_id, terminate=False)
   │           │     for each pending email event
   │           ├── UPDATE email_events SET status='cancelled'
   │           └── Enqueue: notify_telegram(lead_id)
   ├── IMAP STORE: mark processed messages as SEEN
   └── INSERT imap_poll_log (polled_at, messages_checked, matches_found)

7. Celery task: notify_telegram(lead_id)
   ├── SELECT leads WHERE id=lead_id
   ├── aiogram Bot.send_message(
   │       chat_id=TELEGRAM_MANAGER_CHAT_ID,
   │       text="Клиент {name} ({email}) ответил на письмо. Цепочка остановлена."
   │   )
   └── On error: log WARNING, no crash, no retry
```

---

## 7. Configuration (.env variables)

```dotenv
# ── Application ───────────────────────────────────────────────────────────────
APP_HOST=0.0.0.0
APP_PORT=8000
APP_SECRET_KEY=<random-32-bytes-hex>        # used for internal signing
LOG_LEVEL=INFO

# ── Webhook security ──────────────────────────────────────────────────────────
TILDA_WEBHOOK_SECRET=<shared-secret>        # HMAC-SHA256 key from Tilda settings

# ── Admin API ─────────────────────────────────────────────────────────────────
ADMIN_API_TOKEN=<random-token>              # Bearer token for /admin/* endpoints

# ── Database ──────────────────────────────────────────────────────────────────
DATABASE_URL=sqlite+aiosqlite:////data/db.sqlite3

# ── Redis / Celery ────────────────────────────────────────────────────────────
REDIS_URL=redis://redis:6379/0
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/1

# ── AmoCRM ────────────────────────────────────────────────────────────────────
AMOCRM_BASE_URL=https://<subdomain>.amocrm.ru
AMOCRM_CLIENT_ID=<oauth2-client-id>
AMOCRM_CLIENT_SECRET=<oauth2-client-secret>
AMOCRM_REDIRECT_URI=<oauth2-redirect-uri>
AMOCRM_ACCESS_TOKEN=<long-lived-access-token>
AMOCRM_REFRESH_TOKEN=<refresh-token>
AMOCRM_PIPELINE_ID=<pipeline-id>
AMOCRM_STAGE_ID=<stage-id>                 # "Первичный контакт" stage ID

# ── SMTP (outgoing email) ─────────────────────────────────────────────────────
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=noreply@example.com
SMTP_PASSWORD=<smtp-password>
SMTP_FROM_NAME=Company Name
SMTP_FROM_EMAIL=noreply@example.com
SMTP_USE_TLS=true                           # STARTTLS on port 587; set false for SSL 465

# ── IMAP (incoming email polling) ─────────────────────────────────────────────
IMAP_HOST=imap.example.com
IMAP_PORT=993
IMAP_USERNAME=noreply@example.com
IMAP_PASSWORD=<imap-password>
IMAP_MAILBOX=INBOX
IMAP_POLL_INTERVAL_SECONDS=300             # default 5 minutes

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN=<bot-token>
TELEGRAM_MANAGER_CHAT_ID=<chat-id>         # numeric chat/user ID of the manager
```

---

## 8. Security Considerations

### 8.1 Webhook validation
- Tilda signs POST requests with HMAC-SHA256 using a shared secret.
- FastAPI middleware reads `X-Tilda-Signature` header, computes `HMAC-SHA256(body, TILDA_WEBHOOK_SECRET)`, and returns `401` on mismatch.
- Comparison uses `hmac.compare_digest` to prevent timing attacks.
- If Tilda does not send a signature, the endpoint rejects the request.

### 8.2 Rate limiting
- `/webhook/tilda` is rate-limited to **60 requests / minute per IP** using `slowapi` (Redis-backed counter).
- `/admin/*` endpoints are rate-limited to **30 requests / minute per token**.
- Exceeding limits returns `HTTP 429 Too Many Requests`.

### 8.3 Token and credential storage
- All secrets are in `.env` (never committed to VCS); `.env.example` contains only placeholder values.
- AmoCRM access/refresh tokens are loaded from env at startup; token refresh logic (OAuth2) stores updated tokens back to a secrets file on the Docker volume — never in DB plaintext.
- Admin Bearer token is compared with `secrets.compare_digest` to prevent timing attacks.
- SMTP and IMAP passwords are stored only in `.env` / Docker secrets, not in the database.

### 8.4 Database
- SQLite file is stored on a named Docker volume, not in the image layer.
- The `/data` volume is not exposed via any HTTP path.
- No raw SQL string interpolation — all queries use SQLAlchemy parameter binding to prevent SQL injection.

### 8.5 Email
- Outgoing emails include `List-Unsubscribe` header.
- HTML templates are rendered with Jinja2 autoescaping enabled to prevent XSS in template variables.
- No user-supplied content is interpolated into SMTP command strings.

### 8.6 Celery
- Redis broker is not exposed on a public port (internal Docker network only).
- Task arguments contain only integer IDs — sensitive data is fetched fresh from DB inside the task, not serialised into the message.
- Celery result backend uses Redis DB 1 (separate from broker DB 0).

---

## 9. Docker Compose Architecture

### Services

| Service    | Image / Build     | Replicas | Ports         | Volumes       | Role                                      |
|------------|-------------------|----------|---------------|---------------|-------------------------------------------|
| `redis`    | redis:7-alpine    | 1        | internal only | redis-data    | Message broker + result backend           |
| `app`      | ./Dockerfile      | 1        | 8000:8000     | sqlite-data   | FastAPI HTTP server                       |
| `worker`   | ./Dockerfile      | 1        | none          | sqlite-data   | Celery worker — processes all task queues |
| `beat`     | ./Dockerfile      | 1        | none          | sqlite-data   | Celery Beat — periodic task scheduler     |

### Startup order & health checks

```
redis → (app, worker, beat)
```

- `redis`: `HEALTHCHECK CMD redis-cli ping`
- `app`: `HEALTHCHECK CMD curl -f http://localhost:8000/health`
- `worker` and `beat` depend on both `redis` (healthy) and `app` (healthy, to ensure DB is migrated).

### Volumes

```yaml
volumes:
  sqlite-data:    # mounted at /data in app, worker, beat
  redis-data:     # mounted at /data in redis
```

### Networks

All four services share a single internal bridge network `pipeline-net`. No service except `app` binds a host port.

### Entrypoints

```
app:    uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
worker: celery -A app.celery_app worker --loglevel=info --concurrency=4
          --queues=default,email,amocrm,telegram
beat:   celery -A app.celery_app beat --loglevel=info
          --scheduler=celery.beat:PersistentScheduler
          --schedule=/data/celerybeat-schedule
```

### Graceful shutdown

- `worker`: Celery receives `SIGTERM` → finishes in-progress tasks → exits. Docker stop timeout set to `60s` to allow task completion.
- `beat`: Stateless between restarts; schedule state persisted to `/data/celerybeat-schedule` (on the `sqlite-data` volume).
- `app`: Uvicorn handles `SIGTERM` → drains in-flight HTTP requests → exits.

### Dockerfile (multi-stage outline)

```
Stage 1 — builder:
  FROM python:3.11-slim AS builder
  COPY requirements.txt .
  RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

Stage 2 — runtime:
  FROM python:3.11-slim
  COPY --from=builder /install /usr/local
  COPY . /app
  WORKDIR /app
  RUN adduser --disabled-password --no-create-home appuser
  USER appuser
  # CMD is overridden per-service in docker-compose.yml
```

---

## Appendix: Task Queue Design

### Celery queues

| Queue       | Tasks                                    | Priority |
|-------------|------------------------------------------|----------|
| `default`   | `schedule_email_chain`, `imap_poll_inbox`| normal   |
| `amocrm`    | `create_amocrm_deal`                     | high     |
| `email`     | `send_email`                             | normal   |
| `telegram`  | `notify_telegram`                        | low      |

### Retry policy summary

| Task                | Max retries | Backoff strategy             |
|---------------------|-------------|------------------------------|
| `create_amocrm_deal`| 3           | Exponential: 60s, 120s, 240s |
| `send_email`        | 3           | Exponential: 30s, 90s, 270s  |
| `notify_telegram`   | 0           | No retry; log only           |
| `imap_poll_inbox`   | 1           | Fixed: 60s                   |
