import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import aiosmtplib

from app.config import settings

logger = logging.getLogger(__name__)


async def send_html_email(
    to_email: str,
    to_name: str,
    subject: str,
    html_body: str,
) -> None:
    """Send an HTML email via SMTP. SMTP_MODE controls TLS: starttls, ssl, or plain."""
    mode = settings.SMTP_MODE.lower()
    logger.debug(
        "[smtp] Connecting to %s:%d mode=%s",
        settings.SMTP_HOST,
        settings.SMTP_PORT,
        mode,
    )

    msg = MIMEMultipart("alternative")
    msg["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
    msg["To"] = f"{to_name} <{to_email}>"
    msg["Subject"] = subject
    msg["List-Unsubscribe"] = f"<mailto:unsubscribe@{settings.SMTP_FROM_EMAIL.split('@')[-1]}>"

    msg.attach(MIMEText(html_body, "html", "utf-8"))

    smtp_kwargs = dict(
        hostname=settings.SMTP_HOST,
        port=settings.SMTP_PORT,
        username=settings.SMTP_USERNAME,
        password=settings.SMTP_PASSWORD,
    )

    try:
        if mode == "starttls":
            # STARTTLS upgrade on port 587
            await aiosmtplib.send(msg, **smtp_kwargs, start_tls=True)
        elif mode == "ssl":
            # Implicit SSL on port 465
            await aiosmtplib.send(msg, **smtp_kwargs, use_tls=True)
        elif mode == "plain":
            # No TLS — use only for local/dev SMTP relays
            await aiosmtplib.send(msg, **smtp_kwargs)
        else:
            raise ValueError(f"Unknown SMTP_MODE={mode!r}; expected starttls, ssl, or plain")
        logger.info('[smtp] Email sent to %s subject="%s"', to_email, subject)
    except Exception as exc:
        logger.error("[smtp] Send failed to %s: %s", to_email, exc, exc_info=True)
        raise
