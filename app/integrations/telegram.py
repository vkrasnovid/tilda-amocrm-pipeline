import logging

from aiogram import Bot

from app.config import settings

logger = logging.getLogger(__name__)


async def send_telegram_message(text: str) -> None:
    """Send a message to the manager Telegram chat."""
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    try:
        await bot.send_message(chat_id=settings.TELEGRAM_MANAGER_CHAT_ID, text=text)
        logger.info("[telegram] Message sent to chat_id=%d", settings.TELEGRAM_MANAGER_CHAT_ID)
    finally:
        await bot.session.close()
