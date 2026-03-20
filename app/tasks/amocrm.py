import asyncio
import logging

from sqlalchemy import select, update

from app.celery_app import celery_app
from app.db.models import leads_table
from app.db.session import get_db
from app.integrations.amocrm import AmoCRMClient

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, queue="amocrm", max_retries=3, name="create_amocrm_deal")
def create_amocrm_deal(self, lead_id: int) -> None:
    """Create AmoCRM contact + deal for the given lead_id."""
    logger.info("[task.amocrm] Starting create_amocrm_deal: lead_id=%d", lead_id)
    try:
        asyncio.run(_create_amocrm_deal_async(lead_id))
    except Exception as exc:
        n = self.request.retries
        if n < self.max_retries:
            countdown = 60 * (2 ** n)
            logger.warning("[task.amocrm] Retry %d for lead_id=%d: %s", n + 1, lead_id, exc)
            raise self.retry(exc=exc, countdown=countdown)
        # Final failure — update DB
        logger.error("[task.amocrm] Failed after all retries: lead_id=%d error=%s", lead_id, exc, exc_info=True)
        asyncio.run(_mark_amocrm_failed(lead_id))
        raise


async def _create_amocrm_deal_async(lead_id: int) -> None:
    """Async implementation of the AmoCRM deal creation flow."""
    async with get_db() as conn:
        result = await conn.execute(
            select(
                leads_table.c.name,
                leads_table.c.email,
                leads_table.c.phone,
            ).where(leads_table.c.id == lead_id)
        )
        lead = result.fetchone()

    if not lead:
        raise ValueError(f"Lead not found: lead_id={lead_id}")

    client = AmoCRMClient()
    try:
        contact_id = await client.find_contact_by_email(lead.email)
        if contact_id is None:
            contact_id = await client.create_contact(lead.name, lead.email, lead.phone)

        deal_id = await client.create_deal(lead.name)
        await client.link_contact_to_deal(deal_id, contact_id)
    finally:
        await client.close()

    async with get_db() as conn:
        await conn.execute(
            update(leads_table)
            .where(leads_table.c.id == lead_id)
            .values(
                amocrm_contact_id=contact_id,
                amocrm_deal_id=deal_id,
                amocrm_status="created",
            )
        )

    logger.info(
        "[task.amocrm] AmoCRM deal created: lead_id=%d deal_id=%d contact_id=%d",
        lead_id, deal_id, contact_id,
    )


async def _mark_amocrm_failed(lead_id: int) -> None:
    async with get_db() as conn:
        await conn.execute(
            update(leads_table)
            .where(leads_table.c.id == lead_id)
            .values(amocrm_status="failed")
        )
