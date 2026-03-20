import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.models import leads_table
from app.db.session import get_db
from app.schemas import TildaWebhookPayload, WebhookResponse
from app.security import verify_tilda_signature

logger = logging.getLogger(__name__)

router = APIRouter()


async def upsert_lead(conn: AsyncConnection, payload: TildaWebhookPayload) -> tuple[int, bool]:
    """Insert or update lead by email. Returns (lead_id, is_new)."""
    # Check if lead exists
    result = await conn.execute(
        select(leads_table.c.id, leads_table.c.chain_status).where(
            leads_table.c.email == payload.email
        )
    )
    existing = result.fetchone()

    if existing:
        lead_id = existing.id
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
        return lead_id, False
    else:
        result = await conn.execute(
            insert(leads_table).values(
                email=payload.email,
                name=payload.name,
                phone=payload.phone,
                source_form_id=payload.formid,
            )
        )
        lead_id = result.lastrowid
        return lead_id, True


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
