import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError

from app.celery_app import celery_app
from app.db.models import email_events_table, leads_table
from app.db.session import get_db

logger = logging.getLogger(__name__)

# Email step countdowns in seconds
_STEP_COUNTDOWNS = {1: 0, 2: 172800, 3: 432000}  # 0, 48h, 120h


@celery_app.task(queue="default", name="schedule_email_chain")
def schedule_email_chain(lead_id: int) -> None:
    """Insert email_events rows and schedule the three send_email tasks."""
    logger.info("[task.email_chain] Scheduling 3 emails for lead_id=%d", lead_id)
    asyncio.run(_schedule_email_chain_async(lead_id))


async def _schedule_email_chain_async(lead_id: int) -> None:
    now = datetime.now(timezone.utc)
    scheduled_ats = {
        1: now,
        2: now + timedelta(hours=48),
        3: now + timedelta(hours=120),
    }

    for step, countdown in _STEP_COUNTDOWNS.items():
        scheduled_at = scheduled_ats[step]

        # Schedule the send_email Celery task
        task = celery_app.send_task(
            "send_email",
            args=[lead_id, step],
            queue="email",
            countdown=countdown,
        )
        task_id = task.id

        # Insert email_event row with INSERT OR IGNORE for idempotency
        async with get_db() as conn:
            try:
                await conn.execute(
                    insert(email_events_table).values(
                        lead_id=lead_id,
                        step=step,
                        celery_task_id=task_id,
                        status="pending",
                        scheduled_at=scheduled_at,
                    )
                )
                logger.debug(
                    "[task.email_chain] Inserted email_event: step=%d scheduled_at=%s task_id=%s",
                    step, scheduled_at.isoformat(), task_id,
                )
            except IntegrityError:
                # Unique constraint on (lead_id, step) — duplicate, skip
                logger.warning(
                    "[task.email_chain] Duplicate schedule skipped for lead_id=%d step=%d",
                    lead_id, step,
                )


@celery_app.task(bind=True, queue="email", max_retries=3, name="send_email")
def send_email(self, lead_id: int, step: int) -> None:
    """Fetch lead, render email template, send via SMTP, update event status."""
    logger.info("[task.send_email] Starting: lead_id=%d step=%d", lead_id, step)
    try:
        asyncio.run(_send_email_async(lead_id, step))
    except Exception as exc:
        n = self.request.retries
        if n < self.max_retries:
            countdown = 30 * (3 ** n)  # 30s, 90s, 270s
            logger.warning(
                "[task.send_email] Retry %d/3 for lead_id=%d step=%d: %s",
                n + 1, lead_id, step, exc,
            )
            raise self.retry(exc=exc, countdown=countdown)
        logger.error(
            "[task.send_email] Failed after all retries: lead_id=%d step=%d error=%s",
            lead_id, step, exc, exc_info=True,
        )
        asyncio.run(_mark_email_failed(lead_id, step, str(exc), self.max_retries))
        raise


async def _send_email_async(lead_id: int, step: int) -> None:
    from app.integrations.smtp import send_html_email  # noqa: PLC0415

    async with get_db() as conn:
        # Check lead is still active
        result = await conn.execute(
            select(
                leads_table.c.name,
                leads_table.c.email,
                leads_table.c.chain_status,
            ).where(leads_table.c.id == lead_id)
        )
        lead = result.fetchone()

    if not lead or lead.chain_status != "active":
        chain_status = lead.chain_status if lead else "not_found"
        logger.debug(
            "[task.send_email] Chain not active, skipping: lead_id=%d chain_status=%s",
            lead_id, chain_status,
        )
        return

    async with get_db() as conn:
        # Check email event is not cancelled
        result = await conn.execute(
            select(email_events_table.c.status).where(
                (email_events_table.c.lead_id == lead_id)
                & (email_events_table.c.step == step)
            )
        )
        event = result.fetchone()

    if not event or event.status == "cancelled":
        logger.debug(
            "[task.send_email] Email event cancelled/missing, skipping: lead_id=%d step=%d",
            lead_id, step,
        )
        return

    # Load and render template
    from jinja2 import Environment, FileSystemLoader, select_autoescape  # noqa: PLC0415
    import os  # noqa: PLC0415

    templates_dir = os.path.join(os.path.dirname(__file__), "..", "..", "templates")
    env = Environment(
        loader=FileSystemLoader(templates_dir),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template(f"email_step_{step}.html")
    html_body = template.render(name=lead.name, email=lead.email)

    subject_map = {
        1: "Добро пожаловать!",
        2: "Напоминание",
        3: "Специальное предложение для вас",
    }
    subject = subject_map.get(step, f"Письмо {step}")

    await send_html_email(
        to_email=lead.email,
        to_name=lead.name,
        subject=subject,
        html_body=html_body,
    )

    async with get_db() as conn:
        await conn.execute(
            update(email_events_table)
            .where(
                (email_events_table.c.lead_id == lead_id)
                & (email_events_table.c.step == step)
            )
            .values(status="sent", sent_at=text("CURRENT_TIMESTAMP"))
        )

    logger.info("[task.send_email] Sent step=%d to email=%s", step, lead.email)


async def _mark_email_failed(lead_id: int, step: int, error: str, retry_count: int) -> None:
    async with get_db() as conn:
        await conn.execute(
            update(email_events_table)
            .where(
                (email_events_table.c.lead_id == lead_id)
                & (email_events_table.c.step == step)
            )
            .values(status="failed", error_message=error, retry_count=retry_count)
        )
