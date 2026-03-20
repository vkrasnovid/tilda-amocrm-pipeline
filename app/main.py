import asyncio
import subprocess
from contextlib import asynccontextmanager

from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.limiter import limiter
from app.routers import admin, health, webhook


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, lambda: subprocess.run(["alembic", "upgrade", "head"], check=True)
    )
    yield


app = FastAPI(title="Tilda → AmoCRM Pipeline", lifespan=lifespan)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

app.include_router(webhook.router)
app.include_router(health.router)
app.include_router(admin.router, prefix="/admin")
