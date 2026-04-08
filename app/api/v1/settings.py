"""Settings API routes — thin wrappers around SettingsService."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.core.auth import check_lockout_risk, require_auth, require_role, require_superadmin
from app.core.dependencies import get_settings_service, get_tenants_service
from app.core.exceptions import ForbiddenError
from app.core.schemas import RoleName
from app.services.auth.schemas import UserContext
from app.services.settings.schemas import (
    EffectiveSettings,
    GlobalSettings,
    TenantSettings,
    UserSettings,
)
from app.services.settings.service import SettingsService
from app.services.tenants.service import TenantsService

router = APIRouter()


@router.get("/global", response_model=GlobalSettings)
async def get_global_settings(
    user: UserContext = Depends(require_superadmin),
    svc: SettingsService = Depends(get_settings_service),
) -> GlobalSettings:
    """Get platform-wide settings. Superadmin only."""
    return await svc.get_global()


@router.patch("/global", response_model=GlobalSettings)
async def patch_global_settings(
    body: dict,
    request: Request,
    user: UserContext = Depends(require_superadmin),
    svc: SettingsService = Depends(get_settings_service),
) -> GlobalSettings:
    """Update platform-wide settings. Superadmin only."""
    # Lockout guard: refuse to remove 'local' from auth_providers if no
    # active superadmin has a real local password (would lock everyone out).
    if "auth_providers" in body:
        incoming_providers = body["auth_providers"]
        if isinstance(incoming_providers, list) and "local" not in incoming_providers:
            registry = request.app.state.registry
            has_local_superadmin = await check_lockout_risk(registry)
            if not has_local_superadmin:
                raise ForbiddenError(
                    "Cannot remove 'local' from auth_providers: no active superadmin "
                    "has a local password. Add a local-password superadmin first."
                )
    return await svc.set_global(body, user.user_id)


@router.get("/tenants/{slug}", response_model=TenantSettings)
async def get_tenant_settings(
    slug: str,
    user: UserContext = Depends(require_role(RoleName.ADMIN)),
    tenants_svc: TenantsService = Depends(get_tenants_service),
    svc: SettingsService = Depends(get_settings_service),
) -> TenantSettings:
    """Get settings for a tenant. Tenant admin or superadmin."""
    tenant = await tenants_svc.get_by_slug(slug)
    return await svc.get_tenant(str(tenant.id))


@router.patch("/tenants/{slug}", response_model=TenantSettings)
async def patch_tenant_settings(
    slug: str,
    body: dict,
    user: UserContext = Depends(require_role(RoleName.ADMIN)),
    tenants_svc: TenantsService = Depends(get_tenants_service),
    svc: SettingsService = Depends(get_settings_service),
) -> TenantSettings:
    """Update settings for a tenant. Tenant admin or superadmin."""
    tenant = await tenants_svc.get_by_slug(slug)
    return await svc.set_tenant(str(tenant.id), body, user.user_id)


@router.get("/users/me", response_model=UserSettings)
async def get_my_settings(
    user: UserContext = Depends(require_auth),
    svc: SettingsService = Depends(get_settings_service),
) -> UserSettings:
    """Get the current user's personal settings."""
    return await svc.get_user(str(user.user_id))


@router.patch("/users/me", response_model=UserSettings)
async def patch_my_settings(
    body: dict,
    user: UserContext = Depends(require_auth),
    svc: SettingsService = Depends(get_settings_service),
) -> UserSettings:
    """Update the current user's personal settings."""
    return await svc.set_user(str(user.user_id), body)


@router.get("/effective", response_model=EffectiveSettings)
async def get_effective_settings(
    slug: str | None = None,
    user: UserContext = Depends(require_auth),
    tenants_svc: TenantsService = Depends(get_tenants_service),
    svc: SettingsService = Depends(get_settings_service),
) -> EffectiveSettings:
    """Get merged effective settings (user -> tenant -> global -> defaults).

    Pass ?slug=<tenant-slug> to include tenant-level overrides.
    """
    tenant_id: str | None = None
    if slug is not None:
        tenant = await tenants_svc.get_by_slug(slug)
        tenant_id = str(tenant.id)

    return await svc.get_effective(
        user_id=str(user.user_id),
        tenant_id=tenant_id,
    )
