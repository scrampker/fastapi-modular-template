"""PRIVATE: Items database operations."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.items.models import Item
from app.services.items.schemas import ItemFilter

# Sentinel returned alongside each FTS row.
_NO_HIGHLIGHT = ""


class ItemRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_postgresql(self) -> bool:
        """Return True when the underlying dialect is PostgreSQL."""
        return self._session.bind.dialect.name == "postgresql"  # type: ignore[union-attr]

    def update_search_vector(self, item: Item) -> None:
        """Populate the search_vector column from the item's searchable fields.

        Call this after creating or updating an item, before flushing.
        The column stores plain concatenated text so that:
        - PostgreSQL can feed it to ``to_tsvector()`` via SQL at query time.
        - SQLite can use simple LIKE matching against the same column.
        """
        parts = [item.name or ""]
        if item.description:
            parts.append(item.description)
        item.search_vector = " ".join(parts)

    # ------------------------------------------------------------------
    # FTS search
    # ------------------------------------------------------------------

    async def search_fts(
        self,
        tenant_id: str,
        query: str,
        limit: int = 5,
    ) -> list[tuple[Item, str]]:
        """Search items using PostgreSQL FTS or a SQLite LIKE fallback.

        Returns a list of ``(Item, highlight)`` tuples where *highlight* is
        either a ``ts_headline``-formatted snippet (PostgreSQL) or the raw
        ``name`` field (SQLite).
        """
        if self._is_postgresql():
            return await self._search_fts_pg(tenant_id, query, limit)
        return await self._search_fts_sqlite(tenant_id, query, limit)

    async def _search_fts_pg(
        self,
        tenant_id: str,
        query: str,
        limit: int,
    ) -> list[tuple[Item, str]]:
        """PostgreSQL path: websearch_to_tsquery + ts_rank + ts_headline."""
        tsquery = func.websearch_to_tsquery("english", query)
        tsvector = func.to_tsvector("english", func.coalesce(Item.search_vector, ""))

        stmt = (
            select(
                Item,
                func.ts_headline(
                    "english",
                    func.coalesce(Item.search_vector, ""),
                    tsquery,
                    "StartSel=<mark>, StopSel=</mark>, MaxWords=30, MinWords=10",
                ).label("highlight"),
            )
            .where(Item.tenant_id == tenant_id)
            .where(tsvector.op("@@")(tsquery))
            .order_by(func.ts_rank(tsvector, tsquery).desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        return [(row[0], row[1] or "") for row in rows]

    async def _search_fts_sqlite(
        self,
        tenant_id: str,
        query: str,
        limit: int,
    ) -> list[tuple[Item, str]]:
        """SQLite fallback: LIKE match per query word against search_vector."""
        words = [w for w in query.split() if w]
        if not words:
            return []

        base = select(Item).where(Item.tenant_id == tenant_id)
        for word in words:
            like = f"%{word}%"
            base = base.where(
                or_(Item.name.ilike(like), Item.description.ilike(like))
            )
        base = base.limit(limit)
        items = (await self._session.scalars(base)).all()
        return [(item, item.name) for item in items]

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

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
        self.update_search_vector(item)
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
        self.update_search_vector(item)
        await self._session.flush()
        return item

    async def delete(self, item: Item) -> None:
        await self._session.delete(item)
        await self._session.flush()

    async def list_older_than(self, tenant_id: str, cutoff: datetime) -> list[Item]:
        """Return all items for *tenant_id* created before *cutoff*.

        Used exclusively by the data-retention report — never deletes anything.
        """
        stmt = (
            select(Item)
            .where(Item.tenant_id == tenant_id)
            .where(Item.created_at < cutoff)
            .order_by(Item.created_at)
        )
        result = await self._session.scalars(stmt)
        return list(result.all())
