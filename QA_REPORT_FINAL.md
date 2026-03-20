# QA Final Report — commit 016ab2a

**Date:** 2026-03-20
**Reviewer:** QA Tester (automated re-review)
**Verdict:** ✅ APPROVED

---

## Checklist

### BUG-005 — Startup validation for `change-me` defaults
**Status: FIXED**

`config.py:62–72` adds a `@model_validator(mode="after")` on `Settings` that raises `ValueError`
at import time if `TILDA_WEBHOOK_SECRET` or `ADMIN_API_TOKEN` is empty or equals `"change-me"`.
Since `settings = Settings()` is at module level (line 75), a misconfigured deployment fails
immediately on startup — before any request is served.

Minor observation (not blocking): `APP_SECRET_KEY` still defaults to `"change-me"` and is not
checked by the validator. This was not in scope for this bug; filing for awareness only.

---

### NEW-A — `lastrowid` replaced with `rowcount`
**Status: FIXED — no regression**

`webhook.py:37` now uses `insert_result.rowcount > 0` to detect new rows.
A follow-up `SELECT` (lines 40–45) fetches `lead_id` and `chain_status` unconditionally.
When `is_new=False` (lines 48–60), the lead is always re-activated via an explicit `UPDATE`
regardless of prior `chain_status`. Logic is correct and race-safe.

Minor observation (not blocking): `lead_row.chain_status` is fetched but the value is not used
in any conditional — re-activation is unconditional on `is_new=False`. Dead field access;
harmless.

---

### BUG-006 — Celery tasks use sync sessions (no cross-loop issue)
**Status: FIXED**

`db/session.py:46–61` adds `get_sync_session()` which creates a **fresh** `sa.create_engine`
per call (sync, pure SQLite driver), yielding a connection via `engine.begin()`, and disposes
the engine in `finally`. This eliminates aiosqlite engine sharing across `asyncio.run()` calls.

All four task modules verified:
- `tasks/amocrm.py` — all DB ops via `get_sync_session()`; only AmoCRM HTTP via `asyncio.run()`
- `tasks/email_chain.py` — all DB ops via `get_sync_session()`; only SMTP via `asyncio.run()`
- `tasks/imap.py` — all DB ops via `get_sync_session()`; IMAP async inside `asyncio.run()`
- `tasks/telegram.py` — DB read via `get_sync_session()`; Telegram send via `asyncio.run()`

No cross-event-loop sharing remains.

Minor observation (not blocking): `get_sync_session()` does not set `PRAGMA journal_mode=WAL`
on the fresh engine. WAL mode is a DB-file-level persistent setting; once set by the FastAPI
async engine on first connect, it remains active for all subsequent connections. Unlikely to
cause issues in practice, but worth noting for deployments where the async engine never connects
before Celery tasks run.

---

### NEW-C — `lock_file` NameError
**Status: FIXED**

`integrations/amocrm.py:82` initialises `lock_file = None` before the `try` block.
`finally` (line 146) guards cleanup with `if lock_file is not None:`. If `open()` raises,
the `finally` block exits cleanly without a `NameError`.

---

## New Bugs Introduced
**None found.**

All changes are narrowly scoped to their respective bug fixes. No new imports, dependencies,
or behavioural changes were introduced beyond what the fixes require.

---

## Summary

| Item | Verdict |
|------|---------|
| BUG-005 startup validation | ✅ Fixed |
| NEW-A rowcount vs lastrowid | ✅ Fixed, no regression |
| BUG-006 sync sessions in Celery | ✅ Fixed |
| NEW-C lock_file NameError | ✅ Fixed |
| New bugs introduced | ✅ None |

**Overall verdict: APPROVED** — all four reported issues are correctly resolved with no regressions detected.
