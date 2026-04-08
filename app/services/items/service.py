"""Items service — public interface for item management."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.core.exceptions import ForbiddenError, NotFoundError
from app.core.schemas import AuditContext, PaginatedResponse
from app.services.audit.schemas import AuditLogCreate
from app.services.audit.service import AuditService
from app.services.items.repository import ItemRepository
from app.services.items.schemas import ItemCreate, ItemFilter, ItemRead, ItemUpdate


class ItemsService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        audit: AuditService,
    ) -> None:
        self._session_factory = session_factory
        self._audit = audit

    async def create(
        self, tenant_id: UUID, data: ItemCreate, ctx: AuditContext
    ) -> ItemRead:
        async with self._session_factory() as session:
            repo = ItemRepository(session)
            item = await repo.create(
                tenant_id=str(tenant_id),
                name=data.name,
                description=data.description,
                created_by=str(ctx.user_id),
            )
            await session.commit()
            await self._audit.log(AuditLogCreate(
                user_id=ctx.user_id,
                tenant_id=tenant_id,
                action="item.create",
                target_type="item",
                target_id=str(item.id),
                detail={"name": data.name},
                ip_address=ctx.ip_address,
            ))
            return ItemRead.model_validate(item)

    async def get(self, item_id: UUID, tenant_id: UUID) -> ItemRead:
        async with self._session_factory() as session:
            repo = ItemRepository(session)
            item = await repo.get_by_id(str(item_id))
            if not item:
                raise NotFoundError("Item", str(item_id))
            if item.tenant_id != str(tenant_id):
                raise ForbiddenError("Item not accessible")
            return ItemRead.model_validate(item)

    async def list(
        self, tenant_id: UUID, filters: ItemFilter
    ) -> PaginatedResponse[ItemRead]:
        async with self._session_factory() as session:
            repo = ItemRepository(session)
            records, total = await repo.list(str(tenant_id), filters)
            return PaginatedResponse[ItemRead](
                items=[ItemRead.model_validate(r) for r in records],
                total=total,
                page=filters.page,
                per_page=filters.per_page,
            )

    async def update(
        self, item_id: UUID, tenant_id: UUID, data: ItemUpdate, ctx: AuditContext
    ) -> ItemRead:
        async with self._session_factory() as session:
            repo = ItemRepository(session)
            item = await repo.get_by_id(str(item_id))
            if not item:
                raise NotFoundError("Item", str(item_id))
            if item.tenant_id != str(tenant_id):
                raise ForbiddenError("Item not accessible")
            updates = data.model_dump(exclude_unset=True)
            item = await repo.update(item, **updates)
            await session.commit()
            await self._audit.log(AuditLogCreate(
                user_id=ctx.user_id,
                tenant_id=tenant_id,
                action="item.update",
                target_type="item",
                target_id=str(item_id),
                detail=updates,
                ip_address=ctx.ip_address,
            ))
            return ItemRead.model_validate(item)

    async def delete(
        self, item_id: UUID, tenant_id: UUID, ctx: AuditContext
    ) -> None:
        async with self._session_factory() as session:
            repo = ItemRepository(session)
            item = await repo.get_by_id(str(item_id))
            if not item:
                raise NotFoundError("Item", str(item_id))
            if item.tenant_id != str(tenant_id):
                raise ForbiddenError("Item not accessible")
            await repo.delete(item)
            await session.commit()
            await self._audit.log(AuditLogCreate(
                user_id=ctx.user_id,
                tenant_id=tenant_id,
                action="item.delete",
                target_type="item",
                target_id=str(item_id),
                ip_address=ctx.ip_address,
            ))
