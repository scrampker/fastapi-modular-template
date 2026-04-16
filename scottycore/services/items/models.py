"""PRIVATE: Item SQLAlchemy model — example domain entity."""

from __future__ import annotations

from sqlalchemy import Boolean, Index, String, Text, event, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import Table

from scottycore.core.database import Base, TimestampMixin, new_uuid


class Item(Base, TimestampMixin):
    __tablename__ = "items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Stores concatenated searchable text for FTS.
    # PostgreSQL: queried via to_tsvector() with a GIN index (maintained by app).
    # SQLite: plain text used with LIKE fallback.
    search_vector: Mapped[str | None] = mapped_column(Text, nullable=True)


# GIN index on the tsvector expression — PostgreSQL only.
# Defined outside __table_args__ so we can attach a DDL event listener that
# skips creation on non-PostgreSQL dialects (e.g. SQLite used in tests).
_gin_index = Index(
    "ix_items_search_vector_gin",
    text("to_tsvector('english', coalesce(search_vector, ''))"),
    postgresql_using="gin",
)
_gin_index.table = Item.__table__  # type: ignore[attr-defined]


@event.listens_for(Table, "before_create")
def _skip_gin_index_on_non_pg(target, connection, **kw):  # type: ignore[no-untyped-def]
    """Remove the GIN index from the table's index set on non-PostgreSQL dialects."""
    if target.name == "items" and connection.dialect.name != "postgresql":
        target.indexes.discard(_gin_index)
