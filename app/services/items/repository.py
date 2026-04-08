"""PRIVATE: Items database operations."""

from __future__ import annotations

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.items.models import Item
from app.services.items.schemas import ItemFilter


class ItemRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        tenant_id: str,
        name: str,
        description: str | None,
        created_by: str | None,
    ) -> Item:
        item = Item(
            tenant_id=tenant_id,
            name=name,
            description=description,
            created_by=created_by,
        )
        self._session.add(item)
        await self._session.flush()
        return item

    async def get_by_id(self, item_id: str) -> Item | None:
        return await self._session.get(Item, item_id)

    async def list(
        self, tenant_id: str, filters: ItemFilter
    ) -> tuple[list[Item], int]:
        query = select(Item).where(Item.tenant_id == tenant_id)
        count_query = select(func.count(Item.id)).where(Item.tenant_id == tenant_id)

        if filters.is_active is not None:
            query = query.where(Item.is_active == filters.is_active)
            count_query = count_query.where(Item.is_active == filters.is_active)
        if filters.search:
            like = f"%{filters.search}%"
            query = query.where(Item.name.ilike(like) | Item.description.ilike(like))
            count_query = count_query.where(Item.name.ilike(like) | Item.description.ilike(like))

        total = (await self._session.scalar(count_query)) or 0
        query = query.order_by(Item.name).offset(filters.offset).limit(filters.per_page)
        result = await self._session.scalars(query)
        return list(result.all()), total

    async def update(self, item: Item, **kwargs: object) -> Item:
        for key, value in kwargs.items():
            setattr(item, key, value)
        await self._session.flush()
        return item

    async def delete(self, item: Item) -> None:
        await self._session.delete(item)
        await self._session.flush()
