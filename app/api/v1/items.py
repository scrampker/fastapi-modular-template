"""Items API routes — CRUD for tenant-scoped items."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.api.v1.helpers import build_audit_ctx
from app.core.auth import require_role
from app.core.dependencies import get_items_service, get_tenants_service
from app.core.schemas import PaginatedResponse, RoleName
from app.services.auth.schemas import UserContext
from app.services.items.schemas import ItemCreate, ItemFilter, ItemRead, ItemUpdate
from app.services.items.service import ItemsService
from app.services.tenants.service import TenantsService

router = APIRouter()


@router.get("", response_model=PaginatedResponse[ItemRead])
async def list_items(
    slug: str,
    request: Request,
    search: str | None = None,
    is_active: bool | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=1, le=100),
    user: UserContext = Depends(require_role(RoleName.VIEWER)),
    svc: ItemsService = Depends(get_items_service),
    tenants_svc: TenantsService = Depends(get_tenants_service),
) -> PaginatedResponse[ItemRead]:
    """List items for a tenant."""
    tenant = await tenants_svc.get_by_slug(slug)
    ctx = build_audit_ctx(user, request, tenant.id)
    filters = ItemFilter(search=search, is_active=is_active, page=page, per_page=per_page)
    return await svc.list(tenant.id, filters, ctx)


@router.post("", response_model=ItemRead, status_code=201)
async def create_item(
    slug: str,
    body: ItemCreate,
    request: Request,
    user: UserContext = Depends(require_role(RoleName.ANALYST)),
    svc: ItemsService = Depends(get_items_service),
    tenants_svc: TenantsService = Depends(get_tenants_service),
) -> ItemRead:
    """Create a new item in a tenant. Requires analyst role or higher."""
    tenant = await tenants_svc.get_by_slug(slug)
    ctx = build_audit_ctx(user, request, tenant.id)
    return await svc.create(tenant.id, body, ctx)


@router.get("/{item_id}", response_model=ItemRead)
async def get_item(
    slug: str,
    item_id: UUID,
    request: Request,
    user: UserContext = Depends(require_role(RoleName.VIEWER)),
    svc: ItemsService = Depends(get_items_service),
    tenants_svc: TenantsService = Depends(get_tenants_service),
) -> ItemRead:
    """Get a single item by ID."""
    tenant = await tenants_svc.get_by_slug(slug)
    ctx = build_audit_ctx(user, request, tenant.id)
    return await svc.get(item_id, tenant.id, ctx)


@router.patch("/{item_id}", response_model=ItemRead)
async def update_item(
    slug: str,
    item_id: UUID,
    body: ItemUpdate,
    request: Request,
    user: UserContext = Depends(require_role(RoleName.ANALYST)),
    svc: ItemsService = Depends(get_items_service),
    tenants_svc: TenantsService = Depends(get_tenants_service),
) -> ItemRead:
    """Update an item. Requires analyst role or higher."""
    tenant = await tenants_svc.get_by_slug(slug)
    ctx = build_audit_ctx(user, request, tenant.id)
    return await svc.update(item_id, tenant.id, body, ctx)


@router.delete("/{item_id}", status_code=204)
async def delete_item(
    slug: str,
    item_id: UUID,
    request: Request,
    user: UserContext = Depends(require_role(RoleName.ADMIN)),
    svc: ItemsService = Depends(get_items_service),
    tenants_svc: TenantsService = Depends(get_tenants_service),
) -> None:
    """Delete an item. Requires admin role."""
    tenant = await tenants_svc.get_by_slug(slug)
    ctx = build_audit_ctx(user, request, tenant.id)
    await svc.delete(item_id, tenant.id, ctx)
