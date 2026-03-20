import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import insert, select, update

from app.celery_app import celery_app
from app.db.models import email_events_table, imap_poll_log_table, leads_table
from app.db.session import get_db

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, queue="default", max_retries=1, name="imap_poll_inbox")
def imap_poll_inbox(self) -> None:
    """Periodic task: poll IMAP inbox, match leads, stop chains, notify telegram."""
    polled_at = datetime.now(timezone.utc)
    logger.info("[task.imap] Poll started at %s", polled_at.isoformat())
    try:
        messages_checked, matches_found = asyncio.run(_imap_poll_async())
        logger.info("[task.imap] Checked %d messages, found %d matches", messages_checked, matches_found)
        asyncio.run(_log_poll(polled_at, messages_checked, matches_found, error=None))
    except Exception as exc:
        logger.warning("[task.imap] Poll error: %s", exc, exc_info=True)
        asyncio.run(_log_poll(polled_at, 0, 0, error=str(exc)))
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=60)
        raise


async def _imap_poll_async() -> tuple[int, int]:
    from app.integrations.imap import IMAPClient  # noqa: PLC0415

    async with IMAPClient() as client:
        sender_emails, uids = await client.fetch_unseen_senders()
        messages_checked = len(uids)
        matches_found = 0

        for sender_email, uid in zip(sender_emails, uids):
            matched = await _process_sender(sender_email)
            if matched:
                matches_found += 1

        await client.mark_seen(uids)

    return messages_checked, matches_found


async def _process_sender(sender_email: str) -> bool:
    """Check if sender matches an active lead; stop chain if so. Returns True if matched."""
    async with get_db() as conn:
        result = await conn.execute(
            select(leads_table.c.id, leads_table.c.name, leads_table.c.email).where(
                (leads_table.c.email == sender_email)
                & (leads_table.c.chain_status == "active")
            )
        )
        lead = result.fetchone()

    if not lead:
        return False

    lead_id = lead.id
    logger.info("[task.imap] Chain stopped for lead_id=%d email=%s", lead_id, sender_email)

    async with get_db() as conn:
        # Stop the chain
        await conn.execute(
            update(leads_table)
            .where(leads_table.c.id == lead_id)
            .values(
                chain_status="stopped",
                chain_stopped_at=datetime.now(timezone.utc),
                chain_stop_reason="reply",
            )
        )

        # Get pending email events
        result = await conn.execute(
            select(
                email_events_table.c.id,
                email_events_table.c.celery_task_id,
                email_events_table.c.step,
            ).where(
                (email_events_table.c.lead_id == lead_id)
                & (email_events_table.c.status == "pending")
            )
        )
        pending_events = result.fetchall()

    # Revoke pending Celery tasks
    for event in pending_events:
        if event.celery_task_id:
            celery_app.control.revoke(event.celery_task_id, terminate=True)
            logger.debug(
                "[task.imap] Revoked celery task %s for email_event step=%d",
                event.celery_task_id, event.step,
            )

    # Cancel all pending events in bulk
    if pending_events:
        event_ids = [e.id for e in pending_events]
        async with get_db() as conn:
            await conn.execute(
                update(email_events_table)
                .where(email_events_table.c.id.in_(event_ids))
                .values(status="cancelled")
            )

    # Enqueue telegram notification
    celery_app.send_task("notify_telegram", args=[lead_id], queue="telegram")

    return True


async def _log_poll(
    polled_at: datetime,
    messages_checked: int,
    matches_found: int,
    error: str | None,
) -> None:
    async with get_db() as conn:
        await conn.execute(
            insert(imap_poll_log_table).values(
                polled_at=polled_at,
                messages_checked=messages_checked,
                matches_found=matches_found,
                error=error,
            )
        )
