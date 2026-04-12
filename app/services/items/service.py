"""Items service — public interface for item management."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.core.exceptions import ForbiddenError, NotFoundError
from app.core.schemas import AuditContext, PaginatedResponse
from app.services.audit.schemas import AuditLogCreate
from app.services.audit.service import AuditService
from app.services.items.repository import ItemRepository
from app.services.items.schemas import ItemCreate, ItemFilter, ItemRead, ItemRetentionReport, ItemSearchResult, ItemUpdate

# Sentinel for optional audit context parameters
_NO_CTX = None


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

    async def get(
        self, item_id: UUID, tenant_id: UUID, ctx: AuditContext | None = _NO_CTX
    ) -> ItemRead:
        async with self._session_factory() as session:
            repo = ItemRepository(session)
            item = await repo.get_by_id(str(item_id))
            if not item:
                raise NotFoundError("Item", str(item_id))
            if item.tenant_id != str(tenant_id):
                raise ForbiddenError("Item not accessible")
            result = ItemRead.model_validate(item)

        # Log sensitive data access when caller provides audit context.
        # Modules handling PHI MUST pass ctx so this call is never skipped.
        if ctx is not None:
            await self._audit.log_data_access(
                user_id=ctx.user_id,
                resource_type="item",
                resource_id=str(item_id),
                action="view",
                tenant_id=tenant_id,
                ip_address=ctx.ip_address,
            )
        return result

    async def list(
        self, tenant_id: UUID, filters: ItemFilter, ctx: AuditContext | None = _NO_CTX
    ) -> PaginatedResponse[ItemRead]:
        async with self._session_factory() as session:
            repo = ItemRepository(session)
            records, total = await repo.list(str(tenant_id), filters)
            response = PaginatedResponse[ItemRead](
                items=[ItemRead.model_validate(r) for r in records],
                total=total,
                page=filters.page,
                per_page=filters.per_page,
            )

        # Log sensitive data access when caller provides audit context.
        if ctx is not None:
            await self._audit.log_data_access(
                user_id=ctx.user_id,
                resource_type="item",
                resource_id="list",
                action="list",
                tenant_id=tenant_id,
                ip_address=ctx.ip_address,
            )
        return response

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

    async def search_fts(
        self, tenant_id: UUID, query: str, limit: int = 5
    ) -> list[ItemSearchResult]:
        """Full-text search for items. Used by SearchService."""
        async with self._session_factory() as session:
            repo = ItemRepository(session)
            rows = await repo.search_fts(str(tenant_id), query, limit)
            return [
                ItemSearchResult(
                    item=ItemRead.model_validate(item),
                    highlight=highlight or None,
                )
                for item, highlight in rows
            ]

    async def check_retention(
        self, tenant_id: UUID, retention_days: int
    ) -> ItemRetentionReport:
        """Return items that would be affected by data retention enforcement.

        Items whose ``created_at`` timestamp is older than *retention_days* are
        included in the report.  This method NEVER deletes — it is read-only.
        Callers (e.g. an admin UI or a scheduled report job) decide what to do
        with the result.

        Args:
            tenant_id: Restrict results to this tenant.
            retention_days: Age threshold in days.  Items older than this are
                considered past retention.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        async with self._session_factory() as session:
            repo = ItemRepository(session)
            items = await repo.list_older_than(str(tenant_id), cutoff)
        return ItemRetentionReport(
            tenant_id=tenant_id,
            retention_days=retention_days,
            cutoff_date=cutoff,
            affected_count=len(items),
            affected_items=[ItemRead.model_validate(i) for i in items],
        )
