"""PRIVATE: Settings SQLAlchemy model — single KV table with scope."""

from __future__ import annotations

from sqlalchemy import String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from scottycore.core.database import Base, TimestampMixin, new_uuid


class Setting(Base, TimestampMixin):
    __tablename__ = "settings"
    __table_args__ = (
        UniqueConstraint("scope", "scope_id", "key", name="uq_setting_scope_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    scope: Mapped[str] = mapped_column(
        String(16), nullable=False, index=True
    )  # 'global' | 'tenant' | 'user'
    scope_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )  # NULL for global scope
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
