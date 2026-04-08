"""Tenants service — public interface for tenant management."""

from __future__ import annotations

import hashlib
import re
import secrets
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.core.schemas import AuditContext, PaginatedResponse
from app.services.audit.schemas import AuditLogCreate
from app.services.audit.service import AuditService
from app.services.auth.service import AuthService
from app.services.tenants.repository import TenantRepository
from app.services.tenants.schemas import (
    ApiKeyResponse,
    TenantCreate,
    TenantDetail,
    TenantFilter,
    TenantRead,
    TenantUpdate,
)


def _slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


class TenantsService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        audit: AuditService,
    ) -> None:
        self._session_factory = session_factory
        self._audit = audit

    async def create(self, data: TenantCreate, ctx: AuditContext) -> TenantRead:
        slug = _slugify(data.name)
        async with self._session_factory() as session:
            repo = TenantRepository(session)
            existing = await repo.get_by_slug(slug)
            if existing:
                raise ConflictError(f"Tenant with slug '{slug}' already exists")
            tenant = await repo.create(name=data.name, slug=slug)
            await session.commit()
            await self._audit.log(AuditLogCreate(
                user_id=ctx.user_id,
                action="tenant.create",
                target_type="tenant",
                target_id=str(tenant.id),
                detail={"name": data.name, "slug": slug},
                ip_address=ctx.ip_address,
            ))
            return TenantRead.model_validate(tenant)

    async def get_by_slug(self, slug: str) -> TenantRead:
        async with self._session_factory() as session:
            repo = TenantRepository(session)
            tenant = await repo.get_by_slug(slug)
            if not tenant:
                raise NotFoundError("Tenant", slug)
            return TenantRead.model_validate(tenant)

    async def get_by_id(self, tenant_id: UUID) -> TenantRead:
        async with self._session_factory() as session:
            repo = TenantRepository(session)
            tenant = await repo.get_by_id(str(tenant_id))
            if not tenant:
                raise NotFoundError("Tenant", str(tenant_id))
            return TenantRead.model_validate(tenant)

    async def list(
        self, filters: TenantFilter, allowed_slugs: set[str] | None = None
    ) -> PaginatedResponse[TenantRead]:
        async with self._session_factory() as session:
            repo = TenantRepository(session)
            records, total = await repo.list(filters, allowed_slugs=allowed_slugs)
            return PaginatedResponse[TenantRead](
                items=[TenantRead.model_validate(r) for r in records],
                total=total,
                page=filters.page,
                per_page=filters.per_page,
            )

    async def update(
        self, slug: str, data: TenantUpdate, ctx: AuditContext
    ) -> TenantRead:
        async with self._session_factory() as session:
            repo = TenantRepository(session)
            tenant = await repo.get_by_slug(slug)
            if not tenant:
                raise NotFoundError("Tenant", slug)
            updates = data.model_dump(exclude_unset=True)
            if "name" in updates and updates["name"]:
                updates["slug"] = _slugify(updates["name"])
            tenant = await repo.update(tenant, **updates)
            await session.commit()
            await self._audit.log(AuditLogCreate(
                user_id=ctx.user_id,
                tenant_id=ctx.tenant_id,
                action="tenant.update",
                target_type="tenant",
                target_id=str(tenant.id),
                detail=updates,
                ip_address=ctx.ip_address,
            ))
            return TenantRead.model_validate(tenant)

    async def rotate_api_key(self, slug: str, ctx: AuditContext) -> ApiKeyResponse:
        raw_key = secrets.token_urlsafe(32)
        key_hash = AuthService.hash_api_key(raw_key)
        prefix = hashlib.sha256(raw_key.encode()).hexdigest()[:16]
        async with self._session_factory() as session:
            repo = TenantRepository(session)
            tenant = await repo.get_by_slug(slug)
            if not tenant:
                raise NotFoundError("Tenant", slug)
            await repo.update(tenant, api_key_hash=key_hash, api_key_prefix=prefix)
            await session.commit()
            await self._audit.log(AuditLogCreate(
                user_id=ctx.user_id,
                tenant_id=UUID(tenant.id),
                action="api_key.rotate",
                target_type="tenant",
                target_id=str(tenant.id),
                ip_address=ctx.ip_address,
            ))
            return ApiKeyResponse(api_key=raw_key)
