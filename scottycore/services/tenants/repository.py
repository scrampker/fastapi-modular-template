"""PRIVATE: Tenant database operations."""

from __future__ import annotations

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from scottycore.services.tenants.models import Tenant
from scottycore.services.tenants.schemas import TenantFilter


class TenantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, name: str, slug: str) -> Tenant:
        tenant = Tenant(name=name, slug=slug)
        self._session.add(tenant)
        await self._session.flush()
        return tenant

    async def get_by_id(self, tenant_id: str) -> Tenant | None:
        return await self._session.get(Tenant, tenant_id)

    async def get_by_slug(self, slug: str) -> Tenant | None:
        result = await self._session.scalars(
            select(Tenant).where(Tenant.slug == slug)
        )
        return result.first()

    async def get_by_api_key_prefix(self, prefix: str) -> Tenant | None:
        result = await self._session.scalars(
            select(Tenant).where(Tenant.api_key_prefix == prefix)
        )
        return result.first()

    async def list(
        self, filters: TenantFilter, allowed_slugs: set[str] | None = None
    ) -> tuple[list[Tenant], int]:
        query = select(Tenant)
        count_query = select(func.count(Tenant.id))

        # Tenant isolation: non-superadmins only see their assigned tenants
        if allowed_slugs is not None:
            slug_list = list(allowed_slugs)
            query = query.where(Tenant.slug.in_(slug_list))
            count_query = count_query.where(Tenant.slug.in_(slug_list))

        if filters.is_active is not None:
            query = query.where(Tenant.is_active == filters.is_active)
            count_query = count_query.where(Tenant.is_active == filters.is_active)
        if filters.search:
            like = f"%{filters.search}%"
            query = query.where(Tenant.name.ilike(like))
            count_query = count_query.where(Tenant.name.ilike(like))

        total = (await self._session.scalar(count_query)) or 0
        query = query.order_by(Tenant.name).offset(filters.offset).limit(filters.per_page)
        result = await self._session.scalars(query)
        return list(result.all()), total

    async def update(self, tenant: Tenant, **kwargs: object) -> Tenant:
        for key, value in kwargs.items():
            if value is not None:
                setattr(tenant, key, value)
        await self._session.flush()
        return tenant

    async def search_by_name(self, like: str, limit: int = 5) -> list[Tenant]:
        """Search tenants by ILIKE name pattern."""
        result = await self._session.scalars(
            select(Tenant).where(Tenant.name.ilike(like)).limit(limit)
        )
        return list(result.all())
