"""Shared helpers for API v1 route handlers."""

from __future__ import annotations

from uuid import UUID

from fastapi import Request

from scottycore.core.schemas import AuditContext
from scottycore.services.auth.schemas import UserContext


def build_audit_ctx(
    user: UserContext,
    request: Request,
    tenant_id: UUID | None = None,
) -> AuditContext:
    """Build a real AuditContext from an authenticated UserContext."""
    return AuditContext(
        user_id=user.user_id,
        tenant_id=tenant_id,
        ip_address=request.client.host if request.client else "unknown",
    )
