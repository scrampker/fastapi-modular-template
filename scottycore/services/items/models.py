"""PRIVATE: Item SQLAlchemy model — example domain entity."""

from __future__ import annotations

from sqlalchemy import Boolean, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

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

    __table_args__ = (
        # GIN index on the tsvector expression — only applied on PostgreSQL.
        # Alembic migration creates this conditionally; SQLite ignores it at
        # model-sync time because Index is not emitted for create_all() either.
        Index(
            "ix_items_search_vector_gin",
            text("to_tsvector('english', coalesce(search_vector, ''))"),
            postgresql_using="gin",
        ),
    )
