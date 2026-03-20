import logging
from contextlib import asynccontextmanager, contextmanager

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings

logger = logging.getLogger(__name__)

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=(settings.LOG_LEVEL.upper() == "DEBUG"),
)


@event.listens_for(engine.sync_engine, "connect")
def _set_wal_mode(dbapi_connection, connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


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


@contextmanager
def get_sync_session():
    """Synchronous DB context manager for Celery tasks.

    Creates a fresh SQLite engine+connection each time to avoid sharing the
    module-level AsyncEngine across event loops. aiosqlite does not support
    use from a different event loop than the one that created the engine, so
    Celery tasks (which call asyncio.run() per task) must use this instead.
    """
    sync_url = settings.DATABASE_URL.replace("+aiosqlite", "")
    sync_engine = sa.create_engine(sync_url)
    try:
        with sync_engine.begin() as conn:
            yield conn
    finally:
        sync_engine.dispose()
