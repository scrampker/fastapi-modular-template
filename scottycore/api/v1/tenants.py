"""Tenants API routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query

from scottycore.core.auth import require_role, require_superadmin
from scottycore.core.dependencies import get_items_service, get_settings_service, get_tenants_service
from scottycore.core.exceptions import ForbiddenError
from scottycore.core.schemas import PaginatedResponse, RoleName
from scottycore.services.auth.schemas import UserContext
from scottycore.services.items.schemas import ItemRetentionReport
from scottycore.services.items.service import ItemsService
from scottycore.services.settings.service import SettingsService
from scottycore.services.tenants.schemas import (
    ApiKeyResponse,
    TenantCreate,
    TenantFilter,
    TenantRead,
    TenantUpdate,
)
from scottycore.services.tenants.service import TenantsService

router = APIRouter()


@router.get("", response_model=PaginatedResponse[TenantRead])
async def list_tenants(
    name: str | None = None,
    is_active: bool | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=1, le=100),
    user: UserContext = Depends(require_role(RoleName.VIEWER)),
    svc: TenantsService = Depends(get_tenants_service),
) -> PaginatedResponse[TenantRead]:
    """List tenants. Non-superadmins see only tenants they belong to."""
    filters = TenantFilter(name=name, is_active=is_active, page=page, per_page=per_page)
    result = await svc.list(filters)

    if not user.is_superadmin:
        allowed_slugs = set(user.tenant_roles.keys())
        filtered = [t for t in result.items if t.slug in allowed_slugs]
        return PaginatedResponse[TenantRead](
            items=filtered,
            total=len(filtered),
            page=page,
            per_page=per_page,
        )

    return result


@router.post("", response_model=TenantRead, status_code=201)
async def create_tenant(
    body: TenantCreate,
    user: UserContext = Depends(require_superadmin),
    svc: TenantsService = Depends(get_tenants_service),
) -> TenantRead:
    """Create a new tenant. Superadmin only."""
    return await svc.create(body)


@router.get("/{slug}", response_model=TenantRead)
async def get_tenant(
    slug: str,
    user: UserContext = Depends(require_role(RoleName.VIEWER)),
    svc: TenantsService = Depends(get_tenants_service),
) -> TenantRead:
    """Get a tenant by slug. Non-superadmins must belong to the tenant."""
    if not user.is_superadmin and slug not in user.tenant_roles:
        raise ForbiddenError(f"No access to tenant '{slug}'")
    return await svc.get_by_slug(slug)


@router.patch("/{slug}", response_model=TenantRead)
async def update_tenant(
    slug: str,
    body: TenantUpdate,
    user: UserContext = Depends(require_role(RoleName.ADMIN)),
    svc: TenantsService = Depends(get_tenants_service),
) -> TenantRead:
    """Update a tenant. Superadmin or tenant admin only."""
    if not user.is_superadmin and slug not in user.tenant_roles:
        raise ForbiddenError(f"No access to tenant '{slug}'")
    tenant = await svc.get_by_slug(slug)
    return await svc.update(tenant.id, body)


@router.post("/{slug}/rotate-api-key", response_model=ApiKeyResponse)
async def rotate_api_key(
    slug: str,
    user: UserContext = Depends(require_role(RoleName.ADMIN)),
    svc: TenantsService = Depends(get_tenants_service),
) -> ApiKeyResponse:
    """Rotate the API key for a tenant. Returns the new plaintext key once."""
    if not user.is_superadmin and slug not in user.tenant_roles:
        raise ForbiddenError(f"No access to tenant '{slug}'")
    tenant = await svc.get_by_slug(slug)
    return await svc.rotate_api_key(tenant.id)


@router.delete("/{slug}", status_code=204)
async def deactivate_tenant(
    slug: str,
    user: UserContext = Depends(require_superadmin),
    svc: TenantsService = Depends(get_tenants_service),
) -> None:
    """Deactivate a tenant. Superadmin only."""
    tenant = await svc.get_by_slug(slug)
    await svc.deactivate(tenant.id)


@router.get("/{slug}/retention/report", response_model=ItemRetentionReport)
async def get_retention_report(
    slug: str,
    user: UserContext = Depends(require_role(RoleName.ADMIN)),
    tenants_svc: TenantsService = Depends(get_tenants_service),
    items_svc: ItemsService = Depends(get_items_service),
    settings_svc: SettingsService = Depends(get_settings_service),
) -> ItemRetentionReport:
    """List items that would be affected by data-retention enforcement.

    The retention window is resolved in priority order:
    1. ``TenantSettings.retention_days`` — explicit per-tenant override.
    2. ``TenantSettings.retention_days_override`` — legacy per-tenant field.
    3. ``GlobalSettings.retention_days_default`` — platform default (90 days).

    This endpoint is **read-only** — it never deletes anything.
    Requires admin role for the tenant.
    """
    if not user.is_superadmin and slug not in user.tenant_roles:
        raise ForbiddenError(f"No access to tenant '{slug}'")

    tenant = await tenants_svc.get_by_slug(slug)

    # Resolve retention days: tenant-specific → global default
    retention_days: int = 90  # safe fallback
    try:
        tenant_cfg = await settings_svc.get_tenant(str(tenant.id))
        if tenant_cfg and tenant_cfg.retention_days is not None:
            retention_days = tenant_cfg.retention_days
        elif tenant_cfg and tenant_cfg.retention_days_override is not None:
            retention_days = tenant_cfg.retention_days_override
        else:
            global_cfg = await settings_svc.get_global()
            if global_cfg and hasattr(global_cfg, "retention_days_default"):
                retention_days = global_cfg.retention_days_default
    except Exception:
        pass  # Fall through to safe default

    return await items_svc.check_retention(tenant.id, retention_days)
