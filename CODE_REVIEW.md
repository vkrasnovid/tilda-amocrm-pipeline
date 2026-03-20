# Code Review — Tilda → AmoCRM + Email Chain Integration

**Verdict: REQUEST_CHANGES**

Date: 2026-03-20
Reviewer: Claude Sonnet 4.6
Branch: `main` (commit `1316cbe`)

---

## Summary

Overall the codebase is well-structured and follows the architecture document closely. Type hints are present throughout, logging is disciplined (no secrets logged), HMAC validation uses constant-time comparison, and SQL queries use parameterised SQLAlchemy expressions (no injection vectors found). However, there are two functional bugs that must be fixed before this is production-safe, plus several medium-severity issues.

---

## Findings

### 🔴 CRITICAL — Must fix

---

#### C1 · Race condition: step-1 email never sent
**File:** `app/tasks/email_chain.py:37–66`

`schedule_email_chain` dispatches the Celery task for step 1 (`countdown=0`) **before** the `email_events` row is inserted into the database. With `countdown=0`, the task can be picked up by a worker and execute before the INSERT commits. `_send_email_async` then reads the event row, finds `None`, and returns silently — the welcome email is never sent.

```python
# line 37 — task dispatched NOW
task = celery_app.send_task("send_email", args=[lead_id, step], ...)

# line 48 — DB row inserted LATER (inside a subsequent `async with get_db()`)
await conn.execute(insert(email_events_table).values(...))
```

**Fix:** Insert all three `email_events` rows first, then dispatch tasks.

---

#### C2 · Rate limiting configured but never applied
**File:** `app/main.py:12,23-25` / `app/routers/webhook.py:56`

`slowapi` middleware and limiter are wired up in `main.py`, but **no endpoint has a `@limiter.limit(...)` decorator**. The middleware is inert without per-route limits. The webhook endpoint — the sole public, unauthenticated entry point — is completely unlimited, contrary to the architecture document's security claims.

```python
# main.py — limiter registered but never used
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)

# webhook.py — missing @limiter.limit("X/minute")
@router.post("/webhook/tilda", response_model=WebhookResponse)
async def webhook_tilda(...):
```

**Fix:** Add `@limiter.limit("30/minute")` (or similar) to `webhook_tilda`.

---

### 🟠 HIGH — Should fix before production

---

#### H1 · `subprocess.run` blocks the event loop during startup
**File:** `app/main.py:17`

`subprocess.run(["alembic", "upgrade", "head"])` is a blocking call inside an `async` lifespan function. It blocks the entire asyncio event loop for the duration of the migration. On cold start with a complex migration this stalls all request handling.

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    subprocess.run(["alembic", "upgrade", "head"], check=True)  # blocks loop
    yield
```

No timeout is set either — a hung migration hangs the process indefinitely.

**Fix:** Use `asyncio.create_subprocess_exec` or offload to `loop.run_in_executor`.

---

#### H2 · SQLite WAL mode not enabled; concurrent writers will deadlock
**File:** `app/db/session.py:10-13`

Three separate processes (`app`, `worker`, `beat`) each hold an independent `AsyncEngine` connected to the same SQLite file. SQLite defaults to DELETE journal mode, which serialises writers with file-level locks. Under load, concurrent writes from the worker and the web app will raise `sqlite3.OperationalError: database is locked`.

```python
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=...,
    # missing: connect_args={"check_same_thread": False}, WAL pragma
)
```

**Fix:** Enable WAL mode via an `event.listen` on `engine` to execute `PRAGMA journal_mode=WAL` on each new connection.

---

#### H3 · Token refresh uses absolute URL against a client that already has `base_url`
**File:** `app/integrations/amocrm.py:80-81`

`self._client` is created with `base_url=settings.AMOCRM_BASE_URL`. Inside `_refresh_access_token`, the call constructs an absolute URL again:

```python
resp = await self._client.post(
    f"{settings.AMOCRM_BASE_URL}/oauth2/access_token",  # absolute URL
    ...
)
```

In httpx, absolute URLs passed to a client with a `base_url` are used as-is, so this works, but the result is confusing duplication and it bypasses the retry logic in `_request`. If the token endpoint returns 5xx, there is no retry.

**Fix:** Use a relative path (`"/oauth2/access_token"`) and route the call through `_request`.

---

### 🟡 MEDIUM — Fix before GA

---

#### M1 · Insecure default secrets will silently pass validation
**File:** `app/config.py:13,17,20`

`TILDA_WEBHOOK_SECRET`, `ADMIN_API_TOKEN`, and `APP_SECRET_KEY` all default to `"change-me"`. pydantic-settings will not reject these — a deployment that forgets to set `.env` silently runs with known secrets.

```python
TILDA_WEBHOOK_SECRET: str = "change-me"
ADMIN_API_TOKEN: str = "change-me"
```

**Fix:** Validate at startup that these values are not the default strings (e.g., add a `@model_validator` that raises `ValueError` if any secret equals `"change-me"`).

---

#### M2 · Token file written without restrictive permissions
**File:** `app/integrations/token_store.py:30-32`

`amocrm_tokens.json` is written without setting file permissions. Any process running in the container as the same user can read AmoCRM OAuth tokens.

```python
with tmp_path.open("w") as f:
    json.dump({...}, f)
os.replace(tmp_path, TOKEN_FILE)
# no os.chmod(TOKEN_FILE, 0o600)
```

**Fix:** Add `os.chmod(TOKEN_FILE, 0o600)` after `os.replace`.

---

#### M3 · All UNSEEN messages marked as seen, not just matched leads
**File:** `app/tasks/imap.py:44`

`client.mark_seen(uids)` marks every UNSEEN message as read regardless of whether the sender matched an active lead. Unrelated mail arriving in the same inbox (support tickets, bounce notifications, etc.) will be silently consumed.

```python
await client.mark_seen(uids)  # marks ALL fetched UIDs
```

**Fix:** Collect only the UIDs that matched leads and mark only those as seen.

---

#### M4 · Redis connection leaks in `/health` on error path
**File:** `app/routers/health.py:29-33`

The Redis connection is opened with `aioredis.from_url(...)` and closed with `await r.aclose()`. If `r.ping()` raises an exception, `aclose()` is never called.

```python
r = aioredis.from_url(settings.REDIS_URL)
await r.ping()        # exception here leaks the connection
await r.aclose()
```

**Fix:** Wrap in `try/finally` or use an async context manager.

---

#### M5 · `APP_SECRET_KEY` is defined but never used
**File:** `app/config.py:13`

`APP_SECRET_KEY` is declared in `Settings` and `.env.example`, but is not referenced anywhere in the codebase. If it is intended for future session signing or CSRF, document its purpose; otherwise remove it to avoid confusion.

---

#### M6 · `updated_at` not updated by Celery task DB writes
**File:** `app/tasks/amocrm.py:60-67`, `app/tasks/email_chain.py:158-165`

The `leads_table` column has `onupdate=func.now()`, which SQLAlchemy Core honours when it generates UPDATE statements. However, `tasks/amocrm.py` and `tasks/email_chain.py` both call `update(leads_table).where(...).values(amocrm_status=..., ...)` without including `updated_at`. SQLAlchemy Core **does** attach `onupdate` column values to UPDATE statements — verify this is working as expected in an integration test, because aiosqlite + SQLAlchemy async has had historical bugs with `onupdate` in batch mode.

---

### 🔵 LOW — Code quality / style

---

#### L1 · Python base image uses a floating tag
**File:** `Dockerfile:2,9`

```dockerfile
FROM python:3.11-slim AS builder
FROM python:3.11-slim
```

Using a floating minor tag means the base image may change across builds. Pin to a specific patch version (e.g., `python:3.11.9-slim`) for reproducible builds.

---

#### L2 · `aiogram` dependency is a range; all others are pinned
**File:** `requirements.txt:11`

```
aiogram>=3.4.1,<4
```

All other dependencies are pinned to exact versions. The loose range for `aiogram` means `pip install` can pick up a newer minor version on the next build, potentially introducing breaking changes.

**Fix:** Pin to a specific version, e.g. `aiogram==3.4.1`.

---

#### L3 · Fragile relative template path resolution
**File:** `app/tasks/email_chain.py:135`

```python
templates_dir = os.path.join(os.path.dirname(__file__), "..", "..", "templates")
```

This path is resolved relative to the `__file__` location at import time. It breaks if the task module is compiled to a `.pyc` file in a different location or if the working directory changes. Use a `pathlib.Path(__file__).resolve().parent.parent.parent / "templates"` or inject the path via config.

---

#### L4 · AMQP-specific queue config used with Redis broker
**File:** `app/celery_app.py:29-34`

```python
task_queues={
    "default": {"exchange": "default", "routing_key": "default"},
    ...
}
```

The `exchange` and `routing_key` fields are AMQP concepts. Redis-backed Celery ignores them. The configuration works but is misleading and adds noise. Use the simpler `task_queues=["default", "amocrm", "email", "telegram"]` for Redis.

---

#### L5 · Missing return type and `**kwargs` annotation
**File:** `app/integrations/amocrm.py:36`

```python
async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
```

`**kwargs` should be `**kwargs: Any` for completeness. This is a minor mypy/type-hint gap.

---

#### L6 · `_bearer_scheme` used as a default argument directly
**File:** `app/security.py:49`

```python
async def verify_admin_token(
    credentials: HTTPAuthorizationCredentials = _bearer_scheme,  # noqa: B008
) -> None:
```

The `# noqa: B008` suppresses the "do not perform function calls in default argument values" warning. The idiomatic FastAPI pattern is `Depends(HTTPBearer())` at the call site. The current approach works but the suppression is a code smell; prefer `Depends` at the router level.

---

#### L7 · Health endpoint always returns HTTP 200 even when degraded
**File:** `app/routers/health.py:38-43`

```python
return {"status": "ok", "db": db_status, "redis": redis_status, ...}
```

The HTTP status code is always 200 even when `db_status == "error"`. Standard practice is to return 503 when a dependency is unhealthy so that load balancers and container health checks can detect and act on the failure. The docker-compose `healthcheck` uses `curl -f` which only checks the HTTP status, so it won't detect a DB failure.

---

## Architecture Adherence

The implementation matches the architecture document in all major areas: 4 queues, 5 API endpoints, 3-step email chain, IMAP polling with Celery Beat, OAuth token refresh, and atomic token file writes. The following minor gaps were found:

| Claim in ARCHITECTURE.md | Reality |
|---|---|
| Rate limiting on webhook | Not applied (C2 above) |
| WAL mode for SQLite concurrent writes | Not configured (H2 above) |

---

## Checklist Summary

| Category | Status |
|---|---|
| PEP 8 / type hints | ✅ Generally good; minor gaps (L5) |
| Architecture adherence | ⚠️ Two gaps (C2, H2) |
| HMAC / token security | ✅ `hmac.compare_digest`, `secrets.compare_digest` used correctly |
| SQL injection | ✅ All queries use SQLAlchemy parameterised expressions |
| XSS in templates | ✅ Jinja2 with `autoescape=select_autoescape(["html"])` |
| Async patterns | ⚠️ Blocking subprocess in lifespan (H1); `asyncio.run()` in tasks is acceptable |
| Celery config | ⚠️ Rate limit decorator missing (C2); AMQP config in Redis context (L4) |
| Docker build | ⚠️ Floating base image tag (L1); no CMD fallback |
| .env / secrets | ⚠️ Insecure defaults not rejected at startup (M1) |
| Alembic migration | ✅ Schema matches model; `render_as_batch=True` for SQLite; downgrade implemented |

---

## Required Changes Before Merge

1. **C1** — Fix race condition in `schedule_email_chain`: insert DB rows before dispatching tasks.
2. **C2** — Add `@limiter.limit("30/minute")` to `webhook_tilda` (or remove slowapi and document no rate limiting).
3. **H1** — Use non-blocking subprocess execution in lifespan, or add a timeout.
4. **H2** — Enable SQLite WAL mode on engine creation.
5. **H3** — Use relative URL in `_refresh_access_token`; route through `_request` for retry coverage.
