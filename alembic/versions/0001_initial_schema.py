"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-03-20

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "leads",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("phone", sa.Text),
        sa.Column("source_form_id", sa.Text),
        sa.Column("amocrm_contact_id", sa.Integer),
        sa.Column("amocrm_deal_id", sa.Integer),
        sa.Column("amocrm_status", sa.Text, server_default="pending"),
        sa.Column("chain_status", sa.Text, server_default="active"),
        sa.Column("chain_stopped_at", sa.DateTime),
        sa.Column("chain_stop_reason", sa.Text),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_leads_email", "leads", ["email"], unique=True)
    op.create_index("idx_leads_chain_status", "leads", ["chain_status"])
    op.create_index("idx_leads_amocrm_deal_id", "leads", ["amocrm_deal_id"])

    op.create_table(
        "email_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("lead_id", sa.Integer, sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("step", sa.Integer, nullable=False),
        sa.Column("celery_task_id", sa.Text),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("scheduled_at", sa.DateTime, nullable=False),
        sa.Column("sent_at", sa.DateTime),
        sa.Column("error_message", sa.Text),
        sa.Column("retry_count", sa.Integer, server_default="0"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_email_events_lead_id", "email_events", ["lead_id"])
    op.create_index("idx_email_events_status", "email_events", ["status"])
    op.create_index("idx_email_events_lead_step", "email_events", ["lead_id", "step"], unique=True)

    op.create_table(
        "imap_poll_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("polled_at", sa.DateTime, nullable=False),
        sa.Column("messages_checked", sa.Integer, nullable=False),
        sa.Column("matches_found", sa.Integer, nullable=False),
        sa.Column("error", sa.Text),
    )


def downgrade() -> None:
    op.drop_table("imap_poll_log")
    op.drop_table("email_events")
    op.drop_table("leads")
