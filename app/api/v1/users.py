"""User management API routes — thin wrappers around UsersService."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from app.api.v1.helpers import build_audit_ctx
from app.core.auth import require_auth, require_role
from app.core.dependencies import get_tenants_service, get_users_service
from app.core.exceptions import NotFoundError
from app.core.schemas import PaginatedResponse, RoleName
from app.services.auth.schemas import UserContext
from app.services.tenants.service import TenantsService
from app.services.users.schemas import (
    PasswordChange,
    ProfileUpdate,
    UserCreate,
    UserFilter,
    UserRead,
    UserUpdate,
    UserWithRole,
)
from app.services.users.service import UsersService

router = APIRouter()
me_router = APIRouter()


# ── Tenant-scoped user management (admin only) ────────────────────────────


@router.get("", response_model=PaginatedResponse[UserWithRole])
async def list_users(
    slug: str,
    is_active: bool | None = None,
    search: str | None = None,
    page: int = 1,
    per_page: int = 25,
    user: UserContext = Depends(require_role(RoleName.ADMIN)),
    svc: UsersService = Depends(get_users_service),
    tenants_svc: TenantsService = Depends(get_tenants_service),
) -> PaginatedResponse[UserWithRole]:
    """List users for a tenant. Requires admin role."""
    tenant = await tenants_svc.get_by_slug(slug)
    filters = UserFilter(is_active=is_active, search=search, page=page, per_page=per_page)
    return await svc.list_for_tenant(tenant.id, filters)


@router.post("", response_model=UserWithRole, status_code=status.HTTP_201_CREATED)
async def create_user(
    slug: str,
    body: UserCreate,
    request: Request,
    user: UserContext = Depends(require_role(RoleName.ADMIN)),
    svc: UsersService = Depends(get_users_service),
    tenants_svc: TenantsService = Depends(get_tenants_service),
) -> UserWithRole:
    """Create a user and assign them to this tenant. Requires admin role."""
    tenant = await tenants_svc.get_by_slug(slug)
    ctx = build_audit_ctx(user, request, tenant.id)
    return await svc.create_for_tenant(tenant.id, body, ctx)


@router.patch("/{user_id}", response_model=UserWithRole)
async def update_user(
    slug: str,
    user_id: UUID,
    body: UserUpdate,
    request: Request,
    user: UserContext = Depends(require_role(RoleName.ADMIN)),
    svc: UsersService = Depends(get_users_service),
    tenants_svc: TenantsService = Depends(get_tenants_service),
) -> UserWithRole:
    """Update a user's role or active status within a tenant. Requires admin role."""
    tenant = await tenants_svc.get_by_slug(slug)
    ctx = build_audit_ctx(user, request, tenant.id)
    return await svc.update_for_tenant(tenant.id, user_id, body, ctx)


@router.delete("/{user_id}", response_model=UserWithRole)
async def deactivate_user(
    slug: str,
    user_id: UUID,
    request: Request,
    user: UserContext = Depends(require_role(RoleName.ADMIN)),
    svc: UsersService = Depends(get_users_service),
    tenants_svc: TenantsService = Depends(get_tenants_service),
) -> UserWithRole:
    """Deactivate a user within a tenant. Requires admin role."""
    tenant = await tenants_svc.get_by_slug(slug)
    ctx = build_audit_ctx(user, request, tenant.id)
    return await svc.deactivate_for_tenant(tenant.id, user_id, ctx)


# ── Current user profile ──────────────────────────────────────────────────


@me_router.get("/me", response_model=UserRead)
async def get_me(
    user: UserContext = Depends(require_auth),
    svc: UsersService = Depends(get_users_service),
) -> UserRead:
    """Get the current user's profile."""
    result = await svc.get_by_id(user.user_id)
    if not result:
        raise NotFoundError("User", str(user.user_id))
    return result


@me_router.patch("/me", response_model=UserRead)
async def update_me(
    body: ProfileUpdate,
    request: Request,
    user: UserContext = Depends(require_auth),
    svc: UsersService = Depends(get_users_service),
) -> UserRead:
    """Update the current user's display name."""
    if not body.display_name:
        result = await svc.get_by_id(user.user_id)
        if not result:
            raise NotFoundError("User", str(user.user_id))
        return result
    ctx = build_audit_ctx(user, request)
    return await svc.update_profile(user.user_id, body.display_name, ctx)


@me_router.post("/me/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_my_password(
    body: PasswordChange,
    request: Request,
    user: UserContext = Depends(require_auth),
    svc: UsersService = Depends(get_users_service),
) -> None:
    """Change the current user's password."""
    ctx = build_audit_ctx(user, request)
    await svc.change_password(user.user_id, body.current_password, body.new_password, ctx)
