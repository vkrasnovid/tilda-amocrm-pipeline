# QA Report v2 — Commit 59b3e3c Re-review

**Date:** 2026-03-20
**Reviewer:** QA Re-analysis
**Commit:** `59b3e3c` — "fix: resolve all critical and high bugs from QA/review"
**Verdict:** ⚠️ **NEEDS_MORE_WORK**

---

## Summary

| Category | Count |
|----------|-------|
| CRITICAL bugs fixed (root cause) | 4/4 ✅ |
| HIGH bugs fixed (root cause) | 5/7 — 1 missed, 1 partial |
| New bugs introduced by fixes | 3 |

---

## CRITICAL Bugs — All 4 Fixed ✅

### BUG-001 — Email sent before DB row exists
**Status: FIXED (root cause)**
`email_chain.py`: DB row is now inserted first, then Celery task is dispatched, then `celery_task_id` is updated. The step-1 race is eliminated.
**Residual concern (LOW):** If `send_task()` succeeds but the subsequent `celery_task_id` UPDATE fails (crash window), the row is stuck `pending` with `celery_task_id=NULL`. The email chain is lost for that step since the `(lead_id, step)` unique constraint prevents re-scheduling. No data corruption, but silent data loss remains possible under crash scenarios.

---

### BUG-002 — Duplicate emails on lead re-activation
**Status: FIXED (root cause)**
`webhook.py` now revokes + deletes all `pending` events before scheduling a new chain. `email_chain.py` adds an atomic `UPDATE WHERE status='pending' → 'sending'` guard to prevent concurrent task claims.
**Dependency risk:** Correctness depends on the `is_new` detection in BUG-004 fix (see NEW-A below).

---

### BUG-003 — AmoCRM token refresh race condition
**Status: FIXED (root cause)**
`amocrm.py` uses `fcntl.flock(LOCK_EX)` run in a thread executor, serializing refresh across processes. After acquiring the lock, stale-token check re-reads from disk and short-circuits if another worker already refreshed.
**Minor defect (LOW):** `lock_file = open(lock_path, "w")` is called **before** the `try` block. If `open()` raises (e.g., `/data` not yet mounted), `lock_file` is undefined when `finally` executes `fcntl.flock(lock_file, LOCK_UN)`, causing a `NameError` that obscures the original exception.

---

### BUG-004 — Concurrent webhooks cause IntegrityError 500
**Status: FIXED (root cause)**
`upsert_lead` now uses `INSERT OR IGNORE` (atomic) followed by a `SELECT` to get `lead_id`, eliminating the SELECT-then-INSERT race entirely.
**See NEW-A below** — the `is_new` detection has a reliability issue.

---

## HIGH Bugs — 5 Fixed, 1 Missed, 1 Partial

### BUG-005 — No startup validation for insecure default secrets
**Status: ❌ NOT FIXED**
`config.py` still contains:
```python
TILDA_WEBHOOK_SECRET: str = "change-me"
ADMIN_API_TOKEN: str = "change-me"
APP_SECRET_KEY: str = "change-me"
AMOCRM_PIPELINE_ID: int = 0
AMOCRM_STAGE_ID: int = 0
```
No `@field_validator` or startup assertion was added. The commit mislabeled the rate-limiting work (a separate code review finding) as "BUG-005/C2", causing the actual BUG-005 to be skipped entirely.
A misconfigured deployment with default secrets is still fully operational — TILDA_WEBHOOK_SECRET forgery and full admin API access remain exploitable.

---

### BUG-006 — SQLite write contention across containers
**Status: ⚠️ PARTIALLY FIXED**
WAL mode is now enabled via `@event.listens_for(engine.sync_engine, "connect")` ✅, and the Alembic migration is now non-blocking (moved to `run_in_executor`) ✅.
**Unresolved (from original report):** The Celery worker uses `asyncio.run()` per task, creating a new event loop each time, while `engine` is a module-level `AsyncEngine` (tied to the first event loop). `aiosqlite` does not support using a connection from a different event loop. Under concurrent Celery workers (`--concurrency=4`), this will trigger `RuntimeError: Task attached to a different loop` or silent corruption. WAL mode reduces contention but does not fix the cross-loop engine sharing.

---

### BUG-007 — Volume permissions: appuser cannot write to /data
**Status: FIXED (root cause)**
`docker-entrypoint.sh` runs as root, executes `mkdir -p /data && chown -R appuser:appuser /data`, then `exec gosu appuser "$@"`. Dockerfile installs `gosu` and sets the entrypoint. Correct fix.

---

### BUG-008 — IMAP search returns sequence numbers, not UIDs
**Status: FIXED (root cause)**
`imap.py` now uses `uid_search()`, `uid("FETCH", ...)`, and `uid("STORE", ...)`. UIDs are stable across concurrent expunges.

---

### BUG-009 — Revoke has no effect on in-flight tasks; TOCTOU gap
**Status: FIXED (root cause)**
`tasks/imap.py`: `terminate=True` now stops running tasks (SIGTERM). `email_chain.py`: atomic `UPDATE WHERE status='pending'` claim prevents TOCTOU duplicate sends. Both aspects of the original bug are addressed.

---

### BUG-010 — subprocess.run() blocks async event loop at startup
**Status: FIXED (root cause)**
`main.py`: migration is now `await loop.run_in_executor(None, lambda: subprocess.run(...))`.

---

### BUG-011 — SMTP TLS configuration inverted
**Status: FIXED (root cause)**
`config.py` replaces `SMTP_USE_TLS: bool` with `SMTP_MODE: str` (`"starttls"` | `"ssl"` | `"plain"`). `smtp.py` maps each value to the correct `aiosmtplib` parameter. Unknown values raise `ValueError` immediately.

---

## New Bugs Introduced by Fixes

### NEW-A — `is_new` detection unreliable in connection pool (MEDIUM)
**File:** `app/routers/webhook.py:47`
```python
is_new = bool(result.lastrowid)
```
Python's `sqlite3` module documents that when an `INSERT OR IGNORE` is ignored (row already exists), `cursor.lastrowid` is **unchanged** from its previous value on that connection. SQLAlchemy's async engine uses a connection pool — a reused connection that previously ran a successful `INSERT` will have a non-zero `lastrowid`. This causes `is_new=True` for an existing lead, skipping the `chain_status="active"` re-activation update and, critically, skipping the old task revocation logic (BUG-002 fix). The result is a regression to duplicate emails for re-activated leads on pooled connections.
**Fix:** Use `result.rowcount > 0` instead of `lastrowid`, or issue a `SELECT changes()` after the INSERT.

---

### NEW-B — Retry resets status to `pending` even after successful send (LOW)
**File:** `app/tasks/email_chain.py:97`
`_reset_email_pending` is called for **any** exception in the `send_email` Celery task, including exceptions that occur after `_send_email_async` has already sent the email and updated the DB to `sent`. In that scenario, `_reset_email_pending` silently resets `status='sent'` back to `'pending'` (the `WHERE status='sending'` clause prevents this, but only if the DB update to `sent` committed first). If the DB `sent` update and the exception both occur in the same transaction boundary gap, the email is resent on retry. This is a narrow race but a regression from the original behavior.

---

### NEW-C — `lock_file` NameError in `finally` if `/data` is unavailable (LOW)
**File:** `app/integrations/amocrm.py:79`
```python
lock_file = open(lock_path, "w")  # before try block
try:
    ...
finally:
    fcntl.flock(lock_file, fcntl.LOCK_UN)  # NameError if open() raised
    lock_file.close()
```
If `open(lock_path, "w")` raises (e.g., `/data` not mounted), Python enters `finally` where `lock_file` is undefined, throwing a secondary `NameError` that replaces the original `FileNotFoundError`. Move `lock_file = open(...)` inside the `try` block, or initialize `lock_file = None` before it and guard the `finally`.

---

## Verdict

**NEEDS_MORE_WORK**

| Issue | Priority |
|-------|----------|
| BUG-005 missed entirely — insecure defaults still in production config | HIGH |
| NEW-A — `is_new` lastrowid unreliability breaks BUG-002/004 fix on pooled connections | MEDIUM |
| BUG-006 partially fixed — aiosqlite cross-loop engine sharing still crashes under Celery concurrency | MEDIUM |
| NEW-B, NEW-C — minor robustness regressions | LOW |

The 4 CRITICAL bugs are genuinely fixed at the root cause. 5 of 7 HIGH bugs are cleanly resolved. However BUG-005 was silently skipped, the NEW-A defect potentially reintroduces the duplicate-email regression under normal pool conditions, and BUG-006's core concurrency issue remains.
