import logging

import redis.asyncio as aioredis
from fastapi import APIRouter
from sqlalchemy import text

from app.config import settings
from app.db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
async def health_check() -> dict:
    """Returns service health: DB connectivity and Redis ping."""
    db_status = "ok"
    redis_status = "ok"

    try:
        async with get_db() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        logger.debug("[health] DB check failed: %s", exc)
        db_status = "error"

    try:
        r = aioredis.from_url(settings.REDIS_URL)
        await r.ping()
        await r.aclose()
    except Exception as exc:
        logger.debug("[health] Redis check failed: %s", exc)
        redis_status = "error"

    logger.debug("[health] DB check: %s, Redis check: %s", db_status, redis_status)

    return {
        "status": "ok",
        "db": db_status,
        "redis": redis_status,
        "version": "1.0.0",
    }
