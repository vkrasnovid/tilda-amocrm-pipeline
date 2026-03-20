import logging
import re

from celery import Celery

from app.config import settings

logger = logging.getLogger(__name__)


def _mask_password(url: str) -> str:
    """Replace :<password>@ with :***@ in a URL."""
    return re.sub(r":([^/@]+)@", ":***@", url)


celery_app = Celery(
    "pipeline",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_queues={
        "default": {"exchange": "default", "routing_key": "default"},
        "amocrm": {"exchange": "amocrm", "routing_key": "amocrm"},
        "email": {"exchange": "email", "routing_key": "email"},
        "telegram": {"exchange": "telegram", "routing_key": "telegram"},
    },
    task_default_queue="default",
    beat_schedule={
        "imap_poll_inbox": {
            "task": "imap_poll_inbox",
            "schedule": settings.IMAP_POLL_INTERVAL_SECONDS,
            "options": {"queue": "default"},
        },
    },
)

# Task modules created in Phases 3-5 are auto-discovered via this call
celery_app.autodiscover_tasks(["app.tasks"])

logger.info(
    "[celery] App started, broker=%s",
    _mask_password(settings.CELERY_BROKER_URL),
)
logger.debug(
    "[celery] Beat schedule entries: %s",
    list(celery_app.conf.beat_schedule.keys()),
)
