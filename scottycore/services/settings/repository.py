"""PRIVATE: Settings database operations."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scottycore.services.settings.models import Setting


class SettingsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, scope: str, scope_id: str | None, key: str) -> Setting | None:
        result = await self._session.scalars(
            select(Setting).where(
                Setting.scope == scope,
                Setting.scope_id == scope_id,
                Setting.key == key,
            )
        )
        return result.first()

    async def upsert(
        self,
        scope: str,
        scope_id: str | None,
        key: str,
        value_json: str,
        updated_by: str | None,
    ) -> Setting:
        existing = await self.get(scope, scope_id, key)
        if existing is not None:
            existing.value_json = value_json
            existing.updated_by = updated_by
            await self._session.flush()
            return existing

        setting = Setting(
            scope=scope,
            scope_id=scope_id,
            key=key,
            value_json=value_json,
            updated_by=updated_by,
        )
        self._session.add(setting)
        await self._session.flush()
        return setting

    async def list_by_scope(self, scope: str, scope_id: str | None) -> list[Setting]:
        result = await self._session.scalars(
            select(Setting).where(
                Setting.scope == scope,
                Setting.scope_id == scope_id,
            )
        )
        return list(result.all())

    async def delete(self, scope: str, scope_id: str | None, key: str) -> None:
        existing = await self.get(scope, scope_id, key)
        if existing is not None:
            await self._session.delete(existing)
            await self._session.flush()
