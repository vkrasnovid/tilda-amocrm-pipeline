import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError

from app.celery_app import celery_app
from app.db.models import email_events_table, leads_table
from app.db.session import get_sync_session

logger = logging.getLogger(__name__)

# Email step countdowns in seconds
_STEP_COUNTDOWNS = {1: 0, 2: 172800, 3: 432000}  # 0, 48h, 120h


@celery_app.task(queue="default", name="schedule_email_chain")
def schedule_email_chain(lead_id: int) -> None:
    """Insert email_events rows and schedule the three send_email tasks."""
    logger.info("[task.email_chain] Scheduling 3 emails for lead_id=%d", lead_id)
    _schedule_email_chain(lead_id)


def _schedule_email_chain(lead_id: int) -> None:
    now = datetime.now(timezone.utc)
    scheduled_ats = {
        1: now,
        2: now + timedelta(hours=48),
        3: now + timedelta(hours=120),
    }

    for step, countdown in _STEP_COUNTDOWNS.items():
        scheduled_at = scheduled_ats[step]

        # BUG-001 fix: Insert the DB row BEFORE dispatching the Celery task so that
        # the worker cannot execute send_email before the row exists.
        with get_sync_session() as conn:
            try:
                conn.execute(
                    insert(email_events_table).values(
                        lead_id=lead_id,
                        step=step,
                        celery_task_id=None,  # updated after dispatch below
                        status="pending",
                        scheduled_at=scheduled_at,
                    )
                )
                logger.debug(
                    "[task.email_chain] Inserted email_event: step=%d scheduled_at=%s",
                    step, scheduled_at.isoformat(),
                )
            except IntegrityError:
                # Unique constraint on (lead_id, step) — row already exists (e.g. step was
                # already sent from a previous chain). Skip dispatching for this step.
                logger.warning(
                    "[task.email_chain] Duplicate schedule skipped for lead_id=%d step=%d",
                    lead_id, step,
                )
                continue

        # Dispatch Celery task now that the DB row is committed.
        task = celery_app.send_task(
            "send_email",
            args=[lead_id, step],
            queue="email",
            countdown=countdown,
        )
        task_id = task.id

        # Store the task_id on the row for later revocation.
        with get_sync_session() as conn:
            conn.execute(
                update(email_events_table)
                .where(
                    (email_events_table.c.lead_id == lead_id)
                    & (email_events_table.c.step == step)
                )
                .values(celery_task_id=task_id)
            )
        logger.debug(
            "[task.email_chain] Dispatched send_email: step=%d task_id=%s",
            step, task_id,
        )


@celery_app.task(bind=True, queue="email", max_retries=3, name="send_email")
def send_email(self, lead_id: int, step: int) -> None:
    """Fetch lead, render email template, send via SMTP, update event status."""
    logger.info("[task.send_email] Starting: lead_id=%d step=%d", lead_id, step)
    try:
        _send_email_sync(lead_id, step)
    except Exception as exc:
        n = self.request.retries
        if n < self.max_retries:
            # Reset the event to "pending" so the next retry attempt can claim it.
            _reset_email_pending(lead_id, step)
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
        _mark_email_failed(lead_id, step, str(exc), self.max_retries)
        raise


def _send_email_sync(lead_id: int, step: int) -> None:
    # 1. Check lead is still active (sync DB)
    with get_sync_session() as conn:
        result = conn.execute(
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

    # BUG-009 fix: atomically claim the event by transitioning pending -> sending.
    # If the row is already cancelled/sent/sending, rowcount will be 0 — skip.
    with get_sync_session() as conn:
        result = conn.execute(
            update(email_events_table)
            .where(
                (email_events_table.c.lead_id == lead_id)
                & (email_events_table.c.step == step)
                & (email_events_table.c.status == "pending")
            )
            .values(status="sending")
        )
        claimed = result.rowcount > 0

    if not claimed:
        logger.debug(
            "[task.send_email] Event not claimable (cancelled/sent/missing): lead_id=%d step=%d",
            lead_id, step,
        )
        return

    # Load and render template
    from jinja2 import Environment, FileSystemLoader, select_autoescape  # noqa: PLC0415

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

    # Send email via async SMTP (asyncio.run creates a fresh event loop per call)
    asyncio.run(_send_html_email_async(lead.email, lead.name, subject, html_body))

    # Update event status to sent (sync DB)
    with get_sync_session() as conn:
        conn.execute(
            update(email_events_table)
            .where(
                (email_events_table.c.lead_id == lead_id)
                & (email_events_table.c.step == step)
            )
            .values(status="sent", sent_at=text("CURRENT_TIMESTAMP"))
        )

    logger.info("[task.send_email] Sent step=%d to email=%s", step, lead.email)


async def _send_html_email_async(
    to_email: str, to_name: str, subject: str, html_body: str
) -> None:
    from app.integrations.smtp import send_html_email  # noqa: PLC0415

    await send_html_email(
        to_email=to_email,
        to_name=to_name,
        subject=subject,
        html_body=html_body,
    )


def _reset_email_pending(lead_id: int, step: int) -> None:
    """Reset event status from 'sending' back to 'pending' before a retry."""
    with get_sync_session() as conn:
        conn.execute(
            update(email_events_table)
            .where(
                (email_events_table.c.lead_id == lead_id)
                & (email_events_table.c.step == step)
                & (email_events_table.c.status == "sending")
            )
            .values(status="pending")
        )


def _mark_email_failed(lead_id: int, step: int, error: str, retry_count: int) -> None:
    with get_sync_session() as conn:
        conn.execute(
            update(email_events_table)
            .where(
                (email_events_table.c.lead_id == lead_id)
                & (email_events_table.c.step == step)
            )
            .values(status="failed", error_message=error, retry_count=retry_count)
        )
