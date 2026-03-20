import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.models import email_events_table, leads_table
from app.db.session import get_db
from app.limiter import limiter
from app.schemas import TildaWebhookPayload, WebhookResponse
from app.security import verify_tilda_signature

logger = logging.getLogger(__name__)

router = APIRouter()


async def upsert_lead(conn: AsyncConnection, payload: TildaWebhookPayload) -> tuple[int, bool]:
    """Insert or update lead by email. Returns (lead_id, is_new).

    BUG-004 fix: uses INSERT OR IGNORE to avoid a SELECT-then-INSERT race that
    crashes with IntegrityError when two concurrent webhooks arrive for the same email.
    """
    # Atomic INSERT OR IGNORE — safe for concurrent requests on the same email.
    result = await conn.execute(
        insert(leads_table).prefix_with("OR IGNORE").values(
            email=payload.email,
            name=payload.name,
            phone=payload.phone,
            source_form_id=payload.formid,
        )
    )
    is_new = bool(result.lastrowid)

    if not is_new:
        # Re-activate existing lead
        await conn.execute(
            update(leads_table)
            .where(leads_table.c.email == payload.email)
            .values(
                name=payload.name,
                phone=payload.phone,
                chain_status="active",
                updated_at=text("CURRENT_TIMESTAMP"),
            )
        )
        logger.warning("[webhook] Duplicate lead re-activated: email=%s", payload.email)

    # Fetch lead_id (works for both new inserts and existing rows).
    result = await conn.execute(
        select(leads_table.c.id).where(leads_table.c.email == payload.email)
    )
    lead_id = result.scalar()
    return lead_id, is_new


@limiter.limit("30/minute")
@router.post("/webhook/tilda", response_model=WebhookResponse)
async def webhook_tilda(
    request: Request,
    _: Any = Depends(verify_tilda_signature),
) -> WebhookResponse:
    """Receives Tilda form submission, upserts lead, enqueues Celery tasks."""
    # Parse payload — support both JSON and form-data
    content_type = request.headers.get("content-type", "")
    try:
        if "application/json" in content_type:
            data = await request.json()
        else:
            form = await request.form()
            data = dict(form)
        payload = TildaWebhookPayload(**data)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    logger.info(
        "[webhook] Received lead: email=%s name=%s form=%s",
        payload.email,
        payload.name,
        payload.formid,
    )

    try:
        async with get_db() as conn:
            lead_id, is_new = await upsert_lead(conn, payload)
    except Exception as exc:
        logger.error(
            "[webhook] DB error upserting lead email=%s: %s",
            payload.email,
            exc,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="Database error") from exc

    logger.info("[webhook] Lead upserted: lead_id=%d (new=%s)", lead_id, is_new)

    # Enqueue Celery tasks — import here to avoid circular imports at module load
    try:
        from app.celery_app import celery_app  # noqa: PLC0415

        if not is_new:
            # BUG-002 fix: revoke and delete old pending email tasks before scheduling a
            # fresh chain, so re-activated leads don't receive duplicate follow-up emails.
            async with get_db() as conn:
                result = await conn.execute(
                    select(
                        email_events_table.c.id,
                        email_events_table.c.celery_task_id,
                    ).where(
                        (email_events_table.c.lead_id == lead_id)
                        & (email_events_table.c.status == "pending")
                    )
                )
                old_pending = result.fetchall()

            for ev in old_pending:
                if ev.celery_task_id:
                    celery_app.control.revoke(ev.celery_task_id, terminate=True)
                    logger.debug(
                        "[webhook] Revoked old task %s for lead_id=%d",
                        ev.celery_task_id, lead_id,
                    )

            if old_pending:
                ids = [ev.id for ev in old_pending]
                async with get_db() as conn:
                    await conn.execute(
                        delete(email_events_table).where(
                            email_events_table.c.id.in_(ids)
                        )
                    )
                logger.debug(
                    "[webhook] Deleted %d old pending event(s) for lead_id=%d",
                    len(ids), lead_id,
                )

        amocrm_task = celery_app.send_task("create_amocrm_deal", args=[lead_id], queue="amocrm")
        email_task = celery_app.send_task("schedule_email_chain", args=[lead_id], queue="default")
        logger.debug(
            "[webhook] Enqueued tasks: amocrm_task_id=%s email_chain_task_id=%s",
            amocrm_task.id,
            email_task.id,
        )
    except Exception as exc:
        logger.error("[webhook] Failed to enqueue tasks for lead_id=%d: %s", lead_id, exc, exc_info=True)
        # Do not fail the request if task enqueueing fails — lead is already saved

    return WebhookResponse(status="ok", lead_id=lead_id)
