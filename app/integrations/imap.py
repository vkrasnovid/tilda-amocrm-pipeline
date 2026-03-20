import email
import logging
from email.header import decode_header
from typing import List

import aioimaplib

from app.config import settings

logger = logging.getLogger(__name__)


def _parse_email_address(from_header: str) -> str:
    """Extract email address from a From: header value."""
    addr = email.utils.parseaddr(from_header)[1]
    return addr.lower().strip()


class IMAPClient:
    def __init__(self) -> None:
        self._client = aioimaplib.IMAP4_SSL(
            host=settings.IMAP_HOST,
            port=settings.IMAP_PORT,
        )
        self._uid_map: dict[str, str] = {}  # email -> uid, for mark_seen

    async def __aenter__(self) -> "IMAPClient":
        logger.debug("[imap] Connecting to %s:%d", settings.IMAP_HOST, settings.IMAP_PORT)
        await self._client.wait_hello_from_server()
        await self._client.login(settings.IMAP_USERNAME, settings.IMAP_PASSWORD)
        await self._client.select(settings.IMAP_MAILBOX)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            await self._client.logout()
        except Exception:
            pass

    async def fetch_unseen_senders(self) -> tuple[list[str], list[str]]:
        """
        Search INBOX for UNSEEN messages.
        Returns (sender_emails, uids) — parallel lists.
        """
        _, data = await self._client.search("UNSEEN", charset=None)
        uid_bytes = data[0]
        if not uid_bytes or uid_bytes == b"":
            logger.debug("[imap] SEARCH UNSEEN returned 0 message(s)")
            return [], []

        uids = uid_bytes.decode().split()
        logger.debug("[imap] SEARCH UNSEEN returned %d message(s)", len(uids))

        sender_emails: list[str] = []
        result_uids: list[str] = []

        for uid in uids:
            try:
                _, msg_data = await self._client.fetch(uid, "(BODY[HEADER.FIELDS (FROM)])")
                raw = msg_data[1]
                if isinstance(raw, (bytes, bytearray)):
                    parsed = email.message_from_bytes(raw)
                    from_val = parsed.get("From", "")
                    sender = _parse_email_address(from_val)
                    if sender:
                        logger.debug("[imap] Parsed sender: %s from UID %s", sender, uid)
                        sender_emails.append(sender)
                        result_uids.append(uid)
            except Exception as exc:
                logger.error("[imap] Connection/fetch error for UID %s: %s", uid, exc, exc_info=True)

        return sender_emails, result_uids

    async def mark_seen(self, uids: list[str]) -> None:
        """Mark messages with given UIDs as SEEN."""
        if not uids:
            return
        uid_list = ",".join(uids)
        await self._client.store(uid_list, "+FLAGS", r"(\Seen)")
        logger.debug("[imap] Marked %d message(s) as SEEN", len(uids))
