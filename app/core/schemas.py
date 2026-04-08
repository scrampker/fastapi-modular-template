"""Shared Pydantic schemas used across all services."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, Field


# ── Enums ──────────────────────────────────────────────────────────────

class RoleName(str, Enum):
    SUPERADMIN = "superadmin"
    ADMIN = "admin"
    ANALYST = "analyst"
    VIEWER = "viewer"


# ── Role hierarchy (higher number = more privilege) ────────────────────

ROLE_HIERARCHY: dict[RoleName, int] = {
    RoleName.VIEWER: 1,
    RoleName.ANALYST: 2,
    RoleName.ADMIN: 3,
    RoleName.SUPERADMIN: 4,
}


def has_minimum_role(user_role: RoleName, minimum: RoleName) -> bool:
    """Check if user_role meets or exceeds the minimum required role."""
    return ROLE_HIERARCHY.get(user_role, 0) >= ROLE_HIERARCHY.get(minimum, 0)


# ── Pagination ─────────────────────────────────────────────────────────

T = TypeVar("T")


class PaginationParams(BaseModel):
    """Reusable pagination input."""
    page: int = Field(default=1, ge=1)
    per_page: int = Field(default=25, ge=1, le=100)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page


class PaginatedResponse(BaseModel, Generic[T]):
    """Reusable paginated output wrapper."""
    items: list[T]
    total: int
    page: int
    per_page: int


# ── Common response schemas ────────────────────────────────────────────

class ErrorResponse(BaseModel):
    """Standard error response body."""
    detail: str
    status_code: int


class HealthResponse(BaseModel):
    """Health check response."""
    status: str = "ok"
    version: str
    environment: str


class TimestampSchema(BaseModel):
    """Mixin for read schemas that include timestamps."""
    created_at: datetime
    updated_at: datetime


class AuditContext(BaseModel):
    """Context passed to services for audit logging."""
    user_id: UUID
    tenant_id: UUID | None = None
    ip_address: str = "unknown"
