"""Audit log API routes."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from scottycore.core.auth import require_role
from scottycore.core.dependencies import get_audit_service, get_tenants_service
from scottycore.core.schemas import PaginatedResponse, RoleName
from scottycore.services.audit.schemas import AuditLogFilter, AuditLogRead
from scottycore.services.audit.service import AuditService
from scottycore.services.auth.schemas import UserContext
from scottycore.services.tenants.service import TenantsService

router = APIRouter()


async def _resolve_tenant_id(
    slug: str,
    tenants_svc: TenantsService = Depends(get_tenants_service),
) -> UUID:
    """Resolve a tenant slug to its UUID; raises 404 if not found."""
    tenant = await tenants_svc.get_by_slug(slug)
    return tenant.id


@router.get("", response_model=PaginatedResponse[AuditLogRead])
async def list_audit_log(
    slug: str,
    user_id: UUID | None = None,
    action: str | None = None,
    target_type: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=1, le=100),
    format: str | None = Query(default=None, description="Set to 'csv' for CSV download"),
    user: UserContext = Depends(require_role(RoleName.ADMIN)),
    tenant_id: UUID = Depends(_resolve_tenant_id),
    svc: AuditService = Depends(get_audit_service),
) -> PaginatedResponse[AuditLogRead] | StreamingResponse:
    """List audit log entries for a tenant. Admin+ only.

    Pass ``format=csv`` to receive a downloadable CSV file instead of JSON.
    """
    filters = AuditLogFilter(
        user_id=user_id,
        action=action,
        target_type=target_type,
        date_from=date_from,
        date_to=date_to,
        page=page,
        per_page=per_page if format != "csv" else 100,
    )

    if format == "csv":
        # For CSV export fetch up to 5000 rows (20 pages of 100) to keep
        # memory bounded while still being useful for audit exports.
        all_items: list[AuditLogRead] = []
        export_filters = AuditLogFilter(
            user_id=user_id,
            action=action,
            target_type=target_type,
            date_from=date_from,
            date_to=date_to,
            page=1,
            per_page=100,
        )
        result = await svc.list_logs(tenant_id, export_filters)
        all_items.extend(result.items)
        total_pages = (result.total + 99) // 100
        for p in range(2, min(total_pages + 1, 51)):
            export_filters = AuditLogFilter(
                user_id=user_id,
                action=action,
                target_type=target_type,
                date_from=date_from,
                date_to=date_to,
                page=p,
                per_page=100,
            )
            batch = await svc.list_logs(tenant_id, export_filters)
            all_items.extend(batch.items)

        return _build_csv_response(all_items, slug)

    return await svc.list_logs(tenant_id, filters)


# ── Internal helpers ───────────────────────────────────────────────────────


def _build_csv_response(items: list[AuditLogRead], slug: str) -> StreamingResponse:
    """Serialise audit log rows to CSV and return as a file download."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["timestamp", "user_id", "action", "target_type", "target_id", "detail", "ip_address"])
    for row in items:
        writer.writerow([
            row.timestamp.isoformat(),
            str(row.user_id),
            row.action,
            row.target_type,
            row.target_id,
            str(row.detail) if row.detail else "",
            row.ip_address,
        ])

    filename = f"audit-log-{slug}-{datetime.utcnow().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
