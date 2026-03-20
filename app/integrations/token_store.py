import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

TOKEN_FILE = Path(os.environ.get("TOKEN_FILE", "/data/amocrm_tokens.json"))


def load_tokens() -> dict:
    """Load access/refresh tokens from TOKEN_FILE. Falls back to env vars if file absent."""
    if TOKEN_FILE.exists():
        try:
            with TOKEN_FILE.open("r") as f:
                data = json.load(f)
            logger.debug("[token_store] Loaded tokens from %s", TOKEN_FILE)
            return data
        except Exception as exc:
            logger.warning("[token_store] Failed to read %s: %s — falling back to env", TOKEN_FILE, exc)
    logger.debug("[token_store] Token file absent, using env vars")
    return {}


def save_tokens(access_token: str, refresh_token: str) -> None:
    """Write tokens atomically to TOKEN_FILE (.tmp then os.replace)."""
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = TOKEN_FILE.with_suffix(".tmp")
    try:
        with tmp_path.open("w") as f:
            json.dump({"access_token": access_token, "refresh_token": refresh_token}, f)
        os.replace(tmp_path, TOKEN_FILE)
        logger.debug("[token_store] Saved tokens to %s", TOKEN_FILE)
    except Exception as exc:
        logger.error("[token_store] Failed to save tokens: %s", exc, exc_info=True)
        raise
