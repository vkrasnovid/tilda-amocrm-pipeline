import asyncio
import fcntl
import logging
from typing import Optional

import httpx

from app.config import settings
from app.integrations.token_store import load_tokens, save_tokens

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_DELAYS = [60, 120, 240]


def _summarise(obj, max_len: int = 200) -> str:
    """Return a short string summary of a dict or str."""
    s = str(obj)
    return s[:max_len] + "..." if len(s) > max_len else s


class AmoCRMClient:
    def __init__(self) -> None:
        tokens = load_tokens()
        self._access_token: str = tokens.get("access_token", settings.AMOCRM_ACCESS_TOKEN)
        self._refresh_token: str = tokens.get("refresh_token", settings.AMOCRM_REFRESH_TOKEN)
        self._client = httpx.AsyncClient(
            base_url=settings.AMOCRM_BASE_URL,
            headers={"Authorization": f"Bearer {self._access_token}"},
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Execute a request with retry and 401 token-refresh logic."""
        for attempt in range(_MAX_RETRIES + 1):
            payload_summary = _summarise(kwargs.get("json") or kwargs.get("data") or "")
            logger.debug("[amocrm] → %s %s payload=%s", method, url, payload_summary)

            try:
                resp = await self._client.request(method, url, **kwargs)
            except (httpx.NetworkError, httpx.TimeoutException) as exc:
                if attempt < _MAX_RETRIES:
                    delay = _RETRY_DELAYS[attempt]
                    logger.warning("[amocrm] Retry %d/3 after error: %s (sleeping %ds)", attempt + 1, exc, delay)
                    await asyncio.sleep(delay)
                    continue
                logger.error("[amocrm] Final failure after 3 retries: %s", exc)
                raise

            logger.debug("[amocrm] ← %d %s", resp.status_code, _summarise(resp.text))

            if resp.status_code == 401 and attempt == 0:
                logger.warning("[amocrm] Access token expired, attempting refresh")
                await self._refresh_access_token()
                continue  # retry with new token

            if resp.status_code >= 500:
                if attempt < _MAX_RETRIES:
                    delay = _RETRY_DELAYS[attempt]
                    logger.warning(
                        "[amocrm] Retry %d/3 after %d error (sleeping %ds)",
                        attempt + 1, resp.status_code, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.error("[amocrm] Final failure after 3 retries: status=%d", resp.status_code)
                resp.raise_for_status()

            resp.raise_for_status()
            return resp

        raise RuntimeError("Unreachable: retry loop exhausted without raising")

    async def _refresh_access_token(self) -> None:
        """Refresh AmoCRM OAuth2 access token with file-based cross-process lock."""
        lock_path = "/data/amocrm_tokens.lock"
        loop = asyncio.get_event_loop()
        lock_file = None
        try:
            lock_file = open(lock_path, "w")  # noqa: WPS515
            # Acquire exclusive file lock — blocks other processes until released.
            # Runs in executor to avoid blocking the async event loop.
            await loop.run_in_executor(
                None, lambda: fcntl.flock(lock_file, fcntl.LOCK_EX)
            )

            # Re-read tokens: another process may have already refreshed while we waited.
            current = load_tokens()
            if current.get("access_token") and current["access_token"] != self._access_token:
                logger.info("[amocrm] Token already refreshed by another process, adopting new tokens")
                self._access_token = current["access_token"]
                self._refresh_token = current["refresh_token"]
                self._client.headers["Authorization"] = f"Bearer {self._access_token}"
                return

            # Perform token refresh with retry on 5xx / network errors.
            resp = None
            for attempt in range(_MAX_RETRIES + 1):
                try:
                    resp = await self._client.post(
                        "/oauth2/access_token",
                        json={
                            "client_id": settings.AMOCRM_CLIENT_ID,
                            "client_secret": settings.AMOCRM_CLIENT_SECRET,
                            "grant_type": "refresh_token",
                            "refresh_token": self._refresh_token,
                            "redirect_uri": settings.AMOCRM_REDIRECT_URI,
                        },
                    )
                    if resp.status_code >= 500 and attempt < _MAX_RETRIES:
                        delay = _RETRY_DELAYS[attempt]
                        logger.warning(
                            "[amocrm] Token refresh 5xx (status=%d), retry %d/%d (sleeping %ds)",
                            resp.status_code, attempt + 1, _MAX_RETRIES, delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    resp.raise_for_status()
                    break
                except (httpx.NetworkError, httpx.TimeoutException) as exc:
                    if attempt < _MAX_RETRIES:
                        delay = _RETRY_DELAYS[attempt]
                        logger.warning(
                            "[amocrm] Token refresh network error, retry %d/%d: %s (sleeping %ds)",
                            attempt + 1, _MAX_RETRIES, exc, delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    logger.error("[amocrm] OAuth2 token refresh failed after retries: %s", exc, exc_info=True)
                    raise

            data = resp.json()
            self._access_token = data["access_token"]
            self._refresh_token = data["refresh_token"]
            save_tokens(self._access_token, self._refresh_token)
            self._client.headers["Authorization"] = f"Bearer {self._access_token}"
            logger.info("[amocrm] Access token refreshed, new tokens stored")
        except Exception as exc:
            logger.error("[amocrm] OAuth2 token refresh failed: %s", exc, exc_info=True)
            raise
        finally:
            if lock_file is not None:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
                lock_file.close()

    async def find_contact_by_email(self, email: str) -> Optional[int]:
        """Search AmoCRM contacts by email. Returns contact_id or None."""
        resp = await self._request("GET", "/api/v4/contacts", params={"query": email})
        data = resp.json()
        embedded = data.get("_embedded", {}).get("contacts", [])
        if embedded:
            contact_id = embedded[0]["id"]
            logger.info("[amocrm] Contact found: contact_id=%d", contact_id)
            return contact_id
        return None

    async def create_contact(self, name: str, email: str, phone: Optional[str] = None) -> int:
        """Create a new contact in AmoCRM. Returns contact_id."""
        custom_fields = [{"field_code": "EMAIL", "values": [{"value": email, "enum_code": "WORK"}]}]
        if phone:
            custom_fields.append({"field_code": "PHONE", "values": [{"value": phone, "enum_code": "WORK"}]})

        resp = await self._request(
            "POST",
            "/api/v4/contacts",
            json=[{"name": name, "custom_fields_values": custom_fields}],
        )
        data = resp.json()
        contact_id = data["_embedded"]["contacts"][0]["id"]
        logger.info("[amocrm] Contact created: contact_id=%d", contact_id)
        return contact_id

    async def create_deal(self, name: str) -> int:
        """Create a new deal (lead) in AmoCRM. Returns deal_id."""
        resp = await self._request(
            "POST",
            "/api/v4/leads",
            json=[{
                "name": name,
                "pipeline_id": settings.AMOCRM_PIPELINE_ID,
                "status_id": settings.AMOCRM_STAGE_ID,
            }],
        )
        data = resp.json()
        deal_id = data["_embedded"]["leads"][0]["id"]
        logger.info("[amocrm] Deal created: deal_id=%d", deal_id)
        return deal_id

    async def link_contact_to_deal(self, deal_id: int, contact_id: int) -> None:
        """Link a contact to a deal in AmoCRM."""
        await self._request(
            "POST",
            f"/api/v4/leads/{deal_id}/links",
            json=[{"to_entity_id": contact_id, "to_entity_type": "contacts"}],
        )
        logger.debug("[amocrm] Linked contact_id=%d to deal_id=%d", contact_id, deal_id)
