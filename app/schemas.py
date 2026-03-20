from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, EmailStr


class TildaWebhookPayload(BaseModel):
    """Tilda form submission payload — handles both JSON and form-data."""
    name: str
    email: EmailStr
    phone: Optional[str] = None
    formid: Optional[str] = None


class WebhookResponse(BaseModel):
    status: str
    lead_id: int


class ErrorResponse(BaseModel):
    status: str = "error"
    detail: str


class EmailEventOut(BaseModel):
    id: int
    lead_id: int
    step: int
    celery_task_id: Optional[str] = None
    status: str
    scheduled_at: datetime
    sent_at: Optional[datetime] = None
    error_message: Optional[str] = None
    retry_count: int
    created_at: datetime


class LeadOut(BaseModel):
    id: int
    email: str
    name: str
    phone: Optional[str] = None
    source_form_id: Optional[str] = None
    amocrm_contact_id: Optional[int] = None
    amocrm_deal_id: Optional[int] = None
    amocrm_status: str
    chain_status: str
    chain_stopped_at: Optional[datetime] = None
    chain_stop_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class LeadDetailOut(LeadOut):
    email_events: List[EmailEventOut] = []
