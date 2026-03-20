import asyncio
import logging

from sqlalchemy import select

from app.celery_app import celery_app
from app.db.models import leads_table
from app.db.session import get_sync_session

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, queue="telegram", max_retries=0, name="notify_telegram")
def notify_telegram(self, lead_id: int) -> None:
    """Fire-and-forget Telegram notification when a lead replies."""
    try:
        # DB read (sync) — avoids cross-loop aiosqlite engine sharing
        with get_sync_session() as conn:
            result = conn.execute(
                select(leads_table.c.name, leads_table.c.email).where(
                    leads_table.c.id == lead_id
                )
            )
            lead = result.fetchone()

        if not lead:
            logger.warning("[task.telegram] Lead not found: lead_id=%d", lead_id)
            return

        logger.info(
            "[task.telegram] Sending notification for lead_id=%d email=%s",
            lead_id, lead.email,
        )

        message = (
            f"Клиент {lead.name} ({lead.email}) ответил на письмо. Цепочка остановлена."
        )
        asyncio.run(_send_telegram_async(message))
    except Exception as exc:
        logger.warning(
            "[task.telegram] Notification failed (no retry): lead_id=%d error=%s",
            lead_id, exc,
        )
        # Do not raise — fire-and-forget, no retry


async def _send_telegram_async(message: str) -> None:
    from app.integrations.telegram import send_telegram_message  # noqa: PLC0415

    await send_telegram_message(message)
