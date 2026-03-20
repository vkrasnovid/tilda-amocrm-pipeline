import logging
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings

logger = logging.getLogger(__name__)

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=(settings.LOG_LEVEL.upper() == "DEBUG"),
)

logger.debug(
    "[db] Engine created: dialect=%s url=%s",
    engine.dialect.name,
    settings.DATABASE_URL,
)


@asynccontextmanager
async def get_db():
    """Async context manager that yields an AsyncConnection."""
    logger.debug("[db] Acquiring DB connection")
    try:
        async with engine.begin() as conn:
            logger.debug("[db] DB connection acquired")
            yield conn
            logger.debug("[db] DB connection released")
    except Exception as exc:
        logger.error("[db] Connection failure: %s", exc, exc_info=True)
        raise
