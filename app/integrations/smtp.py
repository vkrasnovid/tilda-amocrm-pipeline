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
    """Send an HTML email via SMTP (STARTTLS or SSL)."""
    logger.debug(
        "[smtp] Connecting to %s:%d tls=%s",
        settings.SMTP_HOST,
        settings.SMTP_PORT,
        settings.SMTP_USE_TLS,
    )

    msg = MIMEMultipart("alternative")
    msg["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
    msg["To"] = f"{to_name} <{to_email}>"
    msg["Subject"] = subject
    msg["List-Unsubscribe"] = f"<mailto:unsubscribe@{settings.SMTP_FROM_EMAIL.split('@')[-1]}>"

    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        if settings.SMTP_USE_TLS:
            # STARTTLS on port 587
            await aiosmtplib.send(
                msg,
                hostname=settings.SMTP_HOST,
                port=settings.SMTP_PORT,
                username=settings.SMTP_USERNAME,
                password=settings.SMTP_PASSWORD,
                start_tls=True,
            )
        else:
            # SSL on port 465
            await aiosmtplib.send(
                msg,
                hostname=settings.SMTP_HOST,
                port=settings.SMTP_PORT,
                username=settings.SMTP_USERNAME,
                password=settings.SMTP_PASSWORD,
                use_tls=True,
            )
        logger.info('[smtp] Email sent to %s subject="%s"', to_email, subject)
    except Exception as exc:
        logger.error("[smtp] Send failed to %s: %s", to_email, exc, exc_info=True)
        raise
