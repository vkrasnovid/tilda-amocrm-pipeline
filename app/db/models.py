from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    func,
)
from sqlalchemy import ForeignKey

metadata = MetaData()

leads_table = Table(
    "leads",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("email", Text, nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("phone", Text),
    Column("source_form_id", Text),
    Column("amocrm_contact_id", Integer),
    Column("amocrm_deal_id", Integer),
    Column("amocrm_status", Text, server_default="pending"),
    Column("chain_status", Text, server_default="active"),
    Column("chain_stopped_at", DateTime),
    Column("chain_stop_reason", Text),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    Column("updated_at", DateTime, nullable=False, server_default=func.now(), onupdate=func.now()),
    # Indexes
    Index("idx_leads_email", "email", unique=True),
    Index("idx_leads_chain_status", "chain_status"),
    Index("idx_leads_amocrm_deal_id", "amocrm_deal_id"),
)

email_events_table = Table(
    "email_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("lead_id", Integer, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
    Column("step", Integer, nullable=False),
    Column("celery_task_id", Text),
    Column("status", Text, nullable=False, server_default="pending"),
    Column("scheduled_at", DateTime, nullable=False),
    Column("sent_at", DateTime),
    Column("error_message", Text),
    Column("retry_count", Integer, server_default="0"),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    # Indexes
    Index("idx_email_events_lead_id", "lead_id"),
    Index("idx_email_events_status", "status"),
    Index("idx_email_events_lead_step", "lead_id", "step", unique=True),
)

imap_poll_log_table = Table(
    "imap_poll_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("polled_at", DateTime, nullable=False),
    Column("messages_checked", Integer, nullable=False),
    Column("matches_found", Integer, nullable=False),
    Column("error", Text),
)
