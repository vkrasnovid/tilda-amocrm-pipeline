import hmac
import logging
import secrets
from hashlib import sha256

from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings

logger = logging.getLogger(__name__)

_bearer_scheme = HTTPBearer()


async def verify_tilda_signature(request: Request) -> None:
    """FastAPI dependency: validates X-Tilda-Signature HMAC-SHA256 header."""
    signature_header = request.headers.get("X-Tilda-Signature")

    if not signature_header:
        logger.warning(
            "[security] Missing X-Tilda-Signature from %s",
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(status_code=401, detail="Missing X-Tilda-Signature header")

    body = await request.body()
    computed = hmac.new(
        settings.TILDA_WEBHOOK_SECRET.encode(),
        body,
        sha256,
    ).hexdigest()

    logger.debug(
        "[security] HMAC check: header=%s... computed=%s...",
        signature_header[:8],
        computed[:8],
    )

    if not hmac.compare_digest(signature_header, computed):
        logger.warning(
            "[security] Invalid HMAC signature from %s",
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(status_code=401, detail="Invalid signature")


async def verify_admin_token(
    credentials: HTTPAuthorizationCredentials = _bearer_scheme,  # noqa: B008
) -> None:
    """FastAPI dependency: validates Admin Bearer token."""
    if not secrets.compare_digest(credentials.credentials, settings.ADMIN_API_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid admin token")
