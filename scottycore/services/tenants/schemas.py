"""Tenant service contract — schemas only."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from scottycore.core.schemas import PaginationParams


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class TenantUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    is_active: bool | None = None


class TenantRead(BaseModel):
    id: UUID
    name: str
    slug: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TenantDetail(TenantRead):
    """Extended read with counts."""
    user_count: int = 0


class TenantFilter(PaginationParams):
    is_active: bool | None = None
    search: str | None = None


class ApiKeyResponse(BaseModel):
    """Returned ONCE when an API key is generated. Never stored in plaintext."""
    api_key: str
    message: str = "Store this key securely. It cannot be retrieved again."
