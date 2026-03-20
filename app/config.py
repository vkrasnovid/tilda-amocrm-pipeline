import logging
import logging.config

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Application
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    APP_SECRET_KEY: str = "change-me"
    LOG_LEVEL: str = "INFO"

    # Webhook security
    TILDA_WEBHOOK_SECRET: str = "change-me"

    # Admin API
    ADMIN_API_TOKEN: str = "change-me"

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:////data/db.sqlite3"

    # Redis / Celery
    REDIS_URL: str = "redis://redis:6379/0"
    CELERY_BROKER_URL: str = "redis://redis:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://redis:6379/1"

    # AmoCRM
    AMOCRM_BASE_URL: str = "https://example.amocrm.ru"
    AMOCRM_CLIENT_ID: str = ""
    AMOCRM_CLIENT_SECRET: str = ""
    AMOCRM_REDIRECT_URI: str = ""
    AMOCRM_ACCESS_TOKEN: str = ""
    AMOCRM_REFRESH_TOKEN: str = ""
    AMOCRM_PIPELINE_ID: int = 0
    AMOCRM_STAGE_ID: int = 0

    # SMTP
    SMTP_HOST: str = "smtp.example.com"
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = "noreply@example.com"
    SMTP_PASSWORD: str = ""
    SMTP_FROM_NAME: str = "Company Name"
    SMTP_FROM_EMAIL: str = "noreply@example.com"
    SMTP_MODE: str = "starttls"  # "starttls" (port 587), "ssl" (port 465), "plain" (port 25)

    # IMAP
    IMAP_HOST: str = "imap.example.com"
    IMAP_PORT: int = 993
    IMAP_USERNAME: str = "noreply@example.com"
    IMAP_PASSWORD: str = ""
    IMAP_MAILBOX: str = "INBOX"
    IMAP_POLL_INTERVAL_SECONDS: int = 300

    # Telegram
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_MANAGER_CHAT_ID: int = 0


settings = Settings()

# Configure root logger from LOG_LEVEL env var
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="[%(name)s] %(levelname)s %(message)s",
)

logger = logging.getLogger(__name__)

# DEBUG log at startup listing all non-secret setting keys that were loaded
_NON_SECRET_KEYS = [
    "APP_HOST", "APP_PORT", "LOG_LEVEL", "DATABASE_URL", "REDIS_URL",
    "CELERY_BROKER_URL", "CELERY_RESULT_BACKEND", "SMTP_HOST", "SMTP_PORT",
    "SMTP_FROM_EMAIL", "SMTP_FROM_NAME", "SMTP_MODE", "IMAP_HOST",
    "IMAP_PORT", "IMAP_MAILBOX", "IMAP_POLL_INTERVAL_SECONDS",
    "AMOCRM_BASE_URL", "AMOCRM_PIPELINE_ID", "AMOCRM_STAGE_ID",
    "TELEGRAM_MANAGER_CHAT_ID",
]
logger.debug("[config] Loaded settings keys: %s", _NON_SECRET_KEYS)
