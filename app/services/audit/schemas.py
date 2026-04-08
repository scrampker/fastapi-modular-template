"""Audit service contract — schemas only."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.core.schemas import PaginationParams


class AuditLogCreate(BaseModel):
    tenant_id: UUID | None = None
    user_id: UUID
    action: str
    target_type: str
    target_id: str
    detail: dict | None = None
    ip_address: str = "unknown"


class AuditLogRead(BaseModel):
    id: UUID
    tenant_id: UUID | None
    user_id: UUID
    action: str
    target_type: str
    target_id: str
    detail: dict | None
    ip_address: str
    timestamp: datetime

    model_config = {"from_attributes": True}


class AuditLogFilter(PaginationParams):
    user_id: UUID | None = None
    action: str | None = None
    target_type: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
