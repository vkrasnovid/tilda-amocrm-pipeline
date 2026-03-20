# QA Bug Report — Tilda → AmoCRM Pipeline

**Date:** 2026-03-20
**Reviewer:** QA Automated Analysis
**Scope:** All Python files in `app/`, `templates/`, `alembic/`, `Dockerfile`, `docker-compose.yml`

---

## Summary

| Severity | Count |
|----------|-------|
| CRITICAL | 4     |
| HIGH     | 7     |
| MEDIUM   | 7     |
| LOW      | 5     |
| **Total**| **23**|

---

## CRITICAL

---

### BUG-001 — Step-1 email is silently never sent (task enqueued before DB row written)

**File:** `app/tasks/email_chain.py:37–66`
**Severity:** CRITICAL

In `_schedule_email_chain_async`, step 1 has `countdown=0`. The Celery task is dispatched via `celery_app.send_task(...)` **before** the `email_events` row is inserted into the database. The task can begin executing immediately. Inside `_send_email_async`, the task checks:

```python
if not event or event.status == "cancelled":
    return  # silently skips
```

If the task worker picks up the `send_email` task before the DB insert commits, `event` is `None` and the step-1 email is permanently skipped with no error recorded. The DB insert then succeeds, leaving a `pending` row that never transitions to `sent` or `failed`.

**Reproduction:** Under any real workload with a fast worker, step-1 emails will be inconsistently dropped. Under load the window is nearly always hit.

**Fix:** Insert the DB row first, then enqueue the Celery task; or use `countdown >= 1` so the task cannot execute before the transaction commits.

---

### BUG-002 — Re-activated leads receive duplicate emails for all steps

**File:** `app/routers/webhook.py:96–110`, `app/tasks/email_chain.py:18–66`
**Severity:** CRITICAL

When an existing lead re-submits the Tilda form, `upsert_lead` re-activates it (`chain_status="active"`) and enqueues a new `schedule_email_chain` task. This schedules three new `send_email` tasks. The old tasks from the prior activation are **still in the Celery queue** (they were not revoked). The `email_events` unique constraint `(lead_id, step)` causes the new DB inserts to be silently skipped (`IntegrityError`), so the DB still holds old `task_id` values, and old tasks remain unrevoked.

Inside `_send_email_async`, the only guard is:

```python
if not event or event.status == "cancelled":
    return
```

There is **no check for `status == "sent"`**. When both the old and new `send_email` tasks run for steps 2 and 3 (spaced 48 h and 120 h), both proceed to send the email and update the row to `sent`. The lead receives each follow-up email twice.

**Fix:** In `_send_email_async`, guard against `status in ("sent", "cancelled")`; and revoke old pending tasks when re-activating a lead.

---

### BUG-003 — AmoCRM OAuth2 token refresh race condition causes permanent auth failure

**File:** `app/integrations/amocrm.py:55–58, 77–100`, `app/integrations/token_store.py`
**Severity:** CRITICAL

When multiple Celery workers (concurrency=4) all make AmoCRM requests simultaneously and all receive a `401`, each independently calls `_refresh_access_token()`. AmoCRM OAuth2 refresh tokens are **single-use**: the first refresh invalidates the token; subsequent parallel refresh calls fail with `invalid_token`. The last successful refresh's tokens are saved, but all workers that received the failed refresh will continue retrying with the original expired token. If all 4 concurrent refreshes fire within the same window, all can fail, permanently revoking the integration until manual re-authorization.

The atomic file write in `token_store.save_tokens` (`.tmp` + `os.replace`) is safe within one process but offers no cross-process locking. Two containers writing different token JSON files simultaneously with `os.replace` will cause the second write to silently overwrite the first, potentially replacing valid tokens with a stale set.

**Fix:** Use a distributed lock (Redis `SET NX EX`) around the token refresh operation, with a single leader performing the refresh and all followers waiting and re-reading the token file.

---

### BUG-004 — Concurrent webhooks for same email cause unhandled IntegrityError (500)

**File:** `app/routers/webhook.py:18–53`
**Severity:** CRITICAL

`upsert_lead` performs a `SELECT` followed by a conditional `INSERT`. Two concurrent HTTP requests for the same email address can both pass the `SELECT` (finding no row) and both attempt `INSERT`. The second `INSERT` will violate the `UNIQUE` constraint on `leads.email`. This exception is not caught at the insert level; it bubbles up to the outer `except Exception` block in `webhook_tilda`, which logs a DB error and returns `HTTP 500`. One webhook is permanently lost.

SQLite's default transaction isolation (`DEFERRED`) does not prevent this race because the read lock is not held between the `SELECT` and `INSERT`.

**Fix:** Wrap the upsert in an `INSERT OR IGNORE` + `UPDATE` pattern (single atomic statement), or catch `IntegrityError` inside `upsert_lead` and re-query for the existing row.

---

## HIGH

---

### BUG-005 — No startup validation for default/insecure secrets

**File:** `app/config.py:13,17,20`
**Severity:** HIGH

Three security-critical settings default to the literal string `"change-me"`:

- `TILDA_WEBHOOK_SECRET = "change-me"` — an attacker knowing this can forge any webhook payload
- `ADMIN_API_TOKEN = "change-me"` — exposes the full admin API
- `APP_SECRET_KEY = "change-me"` — placeholder with no current use but signals careless deployment

Additionally, `AMOCRM_PIPELINE_ID=0` and `AMOCRM_STAGE_ID=0` are invalid values that will silently create deals in the wrong pipeline/stage without error.

No `@validator` or startup check rejects these defaults. A misconfigured deployment is fully functional but insecure.

**Fix:** Add Pydantic `@field_validator` or a startup assertion that rejects any of the known-insecure default values.

---

### BUG-006 — SQLite concurrent write contention across multiple Docker containers

**File:** `docker-compose.yml:37–77`
**Severity:** HIGH

The `app`, `worker` (4 concurrent Celery threads), and `beat` containers **all mount the same `sqlite-data` volume** at `/data/db.sqlite3`. SQLite is not designed for concurrent multi-process write access. With 4 Celery worker threads each calling `asyncio.run()` on async DB code in parallel, and the FastAPI app independently writing, the database will produce `OperationalError: database is locked` under any real load.

Each `asyncio.run()` in a Celery task creates a fresh event loop, but `get_db()` uses a module-level SQLAlchemy `AsyncEngine`. The engine is shared across `asyncio.run()` calls in different OS threads, which is not supported by `aiosqlite` (it requires the connection to be used within the same event loop that created it).

**Fix:** Either switch to PostgreSQL for multi-process access, or enforce WAL mode + strict serialization, or run a single-threaded Celery worker (`--concurrency=1`).

---

### BUG-007 — Volume permissions: non-root `appuser` cannot write to root-owned `/data`

**File:** `Dockerfile:20–21`, `docker-compose.yml:44`
**Severity:** HIGH

The Dockerfile drops privileges to `appuser` (non-root). Docker named volumes are initialized with root ownership by default. At runtime, `/data/db.sqlite3` and `/data/amocrm_tokens.json` will fail to be created with `PermissionError` unless the volume is explicitly pre-initialized with correct ownership.

The `alembic upgrade head` run at startup (`main.py:17`) will fail with `PermissionError` on a fresh deployment, preventing the application from starting.

**Fix:** Add a `chown -R appuser:appuser /data` step in an entrypoint script, or use a `docker-compose` volume driver with `uid`/`gid` options, or initialize the volume in a one-shot init container.

---

### BUG-008 — IMAP `search()` returns sequence numbers, not UIDs

**File:** `app/integrations/imap.py:45,59,79`
**Severity:** HIGH

`aioimaplib.IMAP4_SSL.search()` returns **IMAP sequence numbers**, not UIDs. The code stores these in a variable named `uids` and uses them in subsequent `fetch()` and `store()` calls. While `fetch` and `store` also operate on sequence numbers (making the code internally consistent), sequence numbers are **not stable**: if any message is expunged or moved by another session while the poll is in progress, all subsequent sequence numbers shift. This causes wrong messages to be fetched and/or marked as SEEN.

The correct approach is to use `uid_search()`, `uid_fetch()`, and `uid_store()` which operate on permanent UIDs.

**Fix:** Replace `search()` with `uid_search()`, `fetch()` with `uid_fetch()`, and `store()` with `uid_store()`.

---

### BUG-009 — Celery `revoke()` has no effect on in-flight `send_email` tasks; TOCTOU gap allows cancelled emails to be sent

**File:** `app/tasks/imap.py:92–98`, `app/tasks/email_chain.py:116–129`
**Severity:** HIGH

`celery_app.control.revoke(task_id, terminate=False)` only prevents a task from starting; it does not stop an already-running task. There is also a TOCTOU race:

1. `send_email` task checks `event.status != "cancelled"` → passes ✓
2. `imap_poll_inbox` runs in another worker: sets `chain_status="stopped"`, `event.status="cancelled"`, calls `revoke()`
3. `send_email` task continues (revoke has no effect on running task): loads template, calls SMTP, sends email
4. DB is updated to `status="sent"` — contradicting the cancelled state

The lead receives an email after explicitly replying and being opted out.

**Fix:** Re-check `event.status` immediately before calling `send_html_email` (inside the same DB transaction that updates the status to `sending`) using a `SELECT ... FOR UPDATE` equivalent, or use an optimistic update (`UPDATE ... WHERE status='pending' RETURNING id`) to atomically claim the send.

---

### BUG-010 — `subprocess.run()` blocks the async event loop during application startup

**File:** `app/main.py:17`
**Severity:** HIGH

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    subprocess.run(["alembic", "upgrade", "head"], check=True)
    yield
```

`subprocess.run()` is a blocking call. Inside an async `lifespan` context manager, this blocks the uvicorn event loop for the entire duration of the migration (potentially seconds). During this time, no other coroutines can run, health checks will time out, and the service is unresponsive.

**Fix:** Use `asyncio.create_subprocess_exec()` and `await proc.wait()`, or run the migration in a thread via `asyncio.get_event_loop().run_in_executor()`.

---

### BUG-011 — SMTP TLS configuration is inverted and misleading

**File:** `app/integrations/smtp.py:35–54`
**Severity:** HIGH

The setting `SMTP_USE_TLS` controls which TLS mode is used, but the semantics are inverted from what the name implies:

- `SMTP_USE_TLS=True` → STARTTLS (`start_tls=True`) — correct for port 587
- `SMTP_USE_TLS=False` → Implicit TLS (`use_tls=True`) — correct for port 465

An operator interpreting `SMTP_USE_TLS=False` as "disable TLS" and configuring `SMTP_PORT=25` will get `aiosmtplib` attempting implicit SSL on an unencrypted port, causing a connection failure — or worse, connecting to a server that silently downgrades.

There is also no plaintext (no-TLS) code path, so it is impossible to configure the service for non-TLS SMTP even if required.

**Fix:** Rename the setting to `SMTP_MODE` with values `"starttls"`, `"ssl"`, `"plain"`, and handle all three cases explicitly.

---

## MEDIUM

---

### BUG-012 — Redis connection leak in health check

**File:** `app/routers/health.py:29–32`
**Severity:** MEDIUM

```python
r = aioredis.from_url(settings.REDIS_URL)
await r.ping()
await r.aclose()
```

If `r.ping()` raises an exception, `r.aclose()` is never called, leaking the connection pool and its underlying socket. Under frequent health check polling (every 15 seconds per `docker-compose.yml`), this accumulates leaked connections.

**Fix:** Use `try/finally`:
```python
r = aioredis.from_url(settings.REDIS_URL)
try:
    await r.ping()
finally:
    await r.aclose()
```

---

### BUG-013 — Rate limiting configured but not applied to any route

**File:** `app/main.py:12–25`, `app/routers/webhook.py`
**Severity:** MEDIUM

`slowapi` is initialized and its middleware is registered, but no route uses the `@limiter.limit()` decorator. The webhook endpoint at `POST /webhook/tilda` is fully open to flooding: an attacker can submit thousands of fake leads per second, exhausting the database and Celery queues with no throttle.

**Fix:** Apply `@limiter.limit("10/minute")` (or appropriate rate) to `webhook_tilda` and other public endpoints.

---

### BUG-014 — IMAP operations have no timeout; Celery task can hang indefinitely

**File:** `app/integrations/imap.py:27–31, 45, 59, 79`
**Severity:** MEDIUM

`aioimaplib.IMAP4_SSL` is instantiated without a `timeout` parameter. All subsequent IMAP operations (`wait_hello_from_server`, `login`, `select`, `search`, `fetch`, `store`, `logout`) have no deadline. If the IMAP server is slow, behind a firewall that drops packets without RST, or returns a partial response, the Celery task hangs forever. With `max_retries=1` and no timeout, the task occupies a worker thread permanently.

Celery Beat will continue scheduling `imap_poll_inbox` every 300 seconds, eventually exhausting all worker slots.

**Fix:** Pass `timeout=30` to `IMAP4_SSL()` and wrap individual operations with `asyncio.wait_for()`.

---

### BUG-015 — Skipped email events remain permanently `pending`

**File:** `app/tasks/email_chain.py:106–112`
**Severity:** MEDIUM

When `_send_email_async` detects `chain_status != "active"` (e.g., chain stopped by IMAP task after Celery revoke failed), it returns early without updating the `email_events` row. The row retains `status="pending"` indefinitely. The admin `/leads/{id}` endpoint shows these events as pending, creating a misleading view of the email chain state.

**Fix:** Update `status="cancelled"` (or `"skipped"`) before returning in the early-exit branches.

---

### BUG-016 — Internal exception message exposed in 422 response body

**File:** `app/routers/webhook.py:72`
**Severity:** MEDIUM

```python
raise HTTPException(status_code=422, detail=str(exc))
```

`str(exc)` for Pydantic `ValidationError` includes field names, received values, and internal class names. This can reveal implementation details (e.g., schema field names, Python package versions) to external callers. Tilda form senders should not receive this level of internal detail.

**Fix:** Log the full exception and return a generic message: `detail="Invalid payload"`.

---

### BUG-017 — `asyncio.run()` per Celery task is incompatible with the shared SQLAlchemy engine

**File:** `app/tasks/amocrm.py:19,28`, `app/tasks/email_chain.py:22,88`, `app/tasks/imap.py:20–25`
**Severity:** MEDIUM

Each Celery task calls `asyncio.run(...)` which creates a new event loop. `get_db()` uses a module-level `AsyncEngine` created at import time with `create_async_engine(...)`. `aiosqlite` connections are tied to the event loop that created the engine. Calling the engine from a different event loop (created by `asyncio.run()` in a new task) is unsupported and will either silently malfunction or raise `RuntimeError: Task attached to a different loop`.

This interacts with BUG-006 and makes the overall database access pattern fundamentally fragile.

**Fix:** Create the engine inside each `asyncio.run()` call, or use Celery's async support (`celery[gevent]`/`asgiref`) with a single shared event loop per worker process.

---

### BUG-018 — Multi-container token file race condition corrupts AmoCRM tokens

**File:** `app/integrations/token_store.py:25–36`
**Severity:** MEDIUM

When multiple worker containers (each running 4 Celery threads) simultaneously write to `/data/amocrm_tokens.json`, the atomic `os.replace()` protects against partial writes within one container but not across containers. Two containers can both write `.tmp` files and then race on `os.replace()`, with the second overwrite silently discarding the first container's valid tokens.

This is distinct from BUG-003 (API-level race) and affects the persistence layer independently.

**Fix:** Use a Redis-based lock (`SET NX EX`) or store tokens in the database, not in a file on a shared volume.

---

## LOW

---

### BUG-019 — `beat` and `worker` services have no Docker health checks

**File:** `docker-compose.yml:37–77`
**Severity:** LOW

The `worker` and `beat` services have no `healthcheck` defined. Docker cannot distinguish a healthy worker from a hung or crashed one. If the beat scheduler dies silently (e.g., corrupted `celerybeat-schedule` file), no IMAP polling will occur, but Docker will keep the container `running`. The `app` service health check (`/health`) does not verify Celery worker availability.

**Fix:** Add `healthcheck` commands using `celery -A app.celery_app inspect ping` or a custom status endpoint.

---

### BUG-020 — Template path resolution is fragile and breaks if run from unexpected working directory

**File:** `app/tasks/email_chain.py:135`
**Severity:** LOW

```python
templates_dir = os.path.join(os.path.dirname(__file__), "..", "..", "templates")
```

This resolves `templates/` relative to `__file__` at import time. It works when the package is installed at `/app/app/tasks/email_chain.py` (as in the Dockerfile), giving `/app/templates`. However, if the `__file__` resolution yields a `.pyc` path, or the code is run from a pip-installed egg, or the working directory is changed, this path silently resolves incorrectly, causing `TemplateNotFound` at send time.

**Fix:** Use `importlib.resources` or resolve the path relative to a known package root set once at application startup.

---

### BUG-021 — Telegram `Bot` instance created per-notification with no pooling

**File:** `app/integrations/telegram.py:12–17`
**Severity:** LOW

A new `aiogram.Bot` instance (and its underlying HTTP session) is created on every `send_telegram_message()` call. Under high notification volume, this creates many short-lived connections. The `finally: await bot.session.close()` ensures cleanup, but the overhead of session creation/teardown per message is unnecessary.

**Fix:** Create a module-level singleton `Bot` instance and reuse it.

---

### BUG-022 — `worker` stop grace period may be insufficient for long-running tasks

**File:** `docker-compose.yml:56`
**Severity:** LOW

`stop_grace_period: 60s` is set for the worker. If a `send_email` task is in the middle of SMTP delivery (which can take up to 30 s per the `aiosmtplib` default) and the worker receives `SIGTERM`, the 60 s window may not be enough if multiple tasks are in-flight simultaneously. After grace period expiry, Docker sends `SIGKILL`, leaving tasks in `STARTED` state with no `failed` status written to the DB.

**Fix:** Set `stop_grace_period: 120s`, and implement Celery's `task_acks_late=True` with `task_reject_on_worker_lost=True` to requeue interrupted tasks.

---

### BUG-023 — `imap_poll_inbox` marks ALL messages seen even if processing was partially interrupted

**File:** `app/tasks/imap.py:44`
**Severity:** LOW

```python
await client.mark_seen(uids)
```

`mark_seen` is called after the processing loop over all senders. If the loop raises an exception mid-way (e.g., DB error on sender N), the exception propagates out of the `async with IMAPClient()` block and `mark_seen` is NOT called — so all messages remain UNSEEN and will be reprocessed next poll. This is actually safe for already-stopped chains (idempotent), but:

1. The Telegram notification will fire again for already-stopped leads.
2. Any sender email for a non-matched lead (processed before the failure point) is safe, but for matched leads that completed before the failure, their chains are already stopped — the duplicate processing will attempt to stop a `stopped` chain, which silently succeeds but generates extra DB writes.

**Fix:** Track which UIDs were successfully processed and call `mark_seen` per-UID immediately after successful processing, rather than batching at the end.

---

## Appendix: Dependency Notes

- `tenacity==8.3.0` is listed in `requirements.txt` but is never imported in any source file. Dead dependency.
- `aiogram>=3.4.1,<4` uses a range pin — acceptable, but the version floor should be tested against the API surface used (`Bot.send_message`, `bot.session.close`).
- `jinja2==3.1.4` with `select_autoescape(["html"])` correctly escapes HTML in templates. No XSS vector found in email templates.
- All SQL queries use SQLAlchemy Core with parameterized bindings. No raw string interpolation into SQL was found. No SQL injection vectors identified.
- HMAC computation in `security.py` correctly uses `hmac.compare_digest` for timing-safe comparison. The `hmac.new()` call is valid Python stdlib API.
