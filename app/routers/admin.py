import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select

from app.db.models import email_events_table, leads_table
from app.db.session import get_db
from app.limiter import limiter
from app.schemas import EmailEventOut, LeadDetailOut, LeadOut
from app.security import verify_admin_token

logger = logging.getLogger(__name__)

router = APIRouter()


@limiter.limit("60/minute")
@router.get("/leads", response_model=List[LeadOut], dependencies=[Depends(verify_admin_token)])
async def list_leads(
    request: Request,
    chain_status: Optional[str] = Query(default=None),
    amocrm_status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> List[LeadOut]:
    """List leads with optional filters and pagination."""
    logger.debug(
        "[admin] GET /leads query: chain_status=%s amocrm_status=%s limit=%d offset=%d",
        chain_status, amocrm_status, limit, offset,
    )

    query = select(leads_table)
    if chain_status:
        query = query.where(leads_table.c.chain_status == chain_status)
    if amocrm_status:
        query = query.where(leads_table.c.amocrm_status == amocrm_status)
    query = query.limit(limit).offset(offset)

    async with get_db() as conn:
        result = await conn.execute(query)
        rows = result.fetchall()

    return [LeadOut(**dict(row._mapping)) for row in rows]


@limiter.limit("60/minute")
@router.get("/leads/{lead_id}", response_model=LeadDetailOut, dependencies=[Depends(verify_admin_token)])
async def get_lead(request: Request, lead_id: int) -> LeadDetailOut:
    """Get a single lead with all its email events."""
    async with get_db() as conn:
        result = await conn.execute(
            select(leads_table).where(leads_table.c.id == lead_id)
        )
        lead_row = result.fetchone()

        if not lead_row:
            logger.info("[admin] GET /leads/%d not found", lead_id)
            raise HTTPException(status_code=404, detail="Lead not found")

        result = await conn.execute(
            select(email_events_table).where(email_events_table.c.lead_id == lead_id)
        )
        event_rows = result.fetchall()

    events = [EmailEventOut(**dict(row._mapping)) for row in event_rows]
    lead_data = dict(lead_row._mapping)
    lead_data["email_events"] = events
    return LeadDetailOut(**lead_data)
