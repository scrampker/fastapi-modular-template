"""Settings service — public interface for multi-tier KV settings."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.settings.repository import SettingsRepository
from app.services.settings.schemas import (
    SMTP_PASSWORD_MASKED,
    TenantSettings,
    EffectiveSettings,
    GlobalSettings,
    UserSettings,
)

_SCOPE_GLOBAL = "global"
_SCOPE_TENANT = "tenant"
_SCOPE_USER = "user"

# Sentinel value used as scope_id for the single global scope row
_GLOBAL_SCOPE_ID: str | None = None


class SettingsService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # ── Global settings ───────────────────────────────────────────────────

    async def get_global(self) -> GlobalSettings:
        async with self._session_factory() as session:
            repo = SettingsRepository(session)
            rows = await repo.list_by_scope(_SCOPE_GLOBAL, _GLOBAL_SCOPE_ID)
            stored = {r.key: json.loads(r.value_json) for r in rows}
            settings = GlobalSettings(**stored)
            # Mask SMTP password on read
            if settings.smtp_password:
                settings = settings.model_copy(update={"smtp_password": SMTP_PASSWORD_MASKED})
            return settings

    async def set_global(self, data: dict, user_id: UUID) -> GlobalSettings:
        # If the caller sends the masked sentinel or empty string, strip it
        incoming = dict(data)
        smtp_pw = incoming.get("smtp_password")
        if smtp_pw is None or smtp_pw == SMTP_PASSWORD_MASKED or smtp_pw == "":
            incoming.pop("smtp_password", None)

        # Read the unmasked current value to merge against
        current_masked = await self.get_global()
        current_dict = current_masked.model_dump()

        # Restore the real smtp_password from the DB
        async with self._session_factory() as session:
            repo = SettingsRepository(session)
            row = await repo.get(_SCOPE_GLOBAL, _GLOBAL_SCOPE_ID, "smtp_password")
            real_smtp_password = json.loads(row.value_json) if row else None
        current_dict["smtp_password"] = real_smtp_password

        merged = {**current_dict, **incoming}
        validated = GlobalSettings(**merged)

        async with self._session_factory() as session:
            repo = SettingsRepository(session)
            for key, value in validated.model_dump().items():
                await repo.upsert(
                    scope=_SCOPE_GLOBAL,
                    scope_id=_GLOBAL_SCOPE_ID,
                    key=key,
                    value_json=json.dumps(value),
                    updated_by=str(user_id),
                )
            await session.commit()

        if validated.smtp_password:
            validated = validated.model_copy(update={"smtp_password": SMTP_PASSWORD_MASKED})
        return validated

    # ── Tenant settings ───────────────────────────────────────────────────

    async def get_tenant(self, tenant_id: str) -> TenantSettings:
        async with self._session_factory() as session:
            repo = SettingsRepository(session)
            rows = await repo.list_by_scope(_SCOPE_TENANT, tenant_id)
            stored = {r.key: json.loads(r.value_json) for r in rows}
            return TenantSettings(**stored)

    async def set_tenant(
        self, tenant_id: str, data: dict, user_id: UUID
    ) -> TenantSettings:
        current = await self.get_tenant(tenant_id)
        merged = {**current.model_dump(), **data}
        validated = TenantSettings(**merged)

        async with self._session_factory() as session:
            repo = SettingsRepository(session)
            for key, value in validated.model_dump().items():
                await repo.upsert(
                    scope=_SCOPE_TENANT,
                    scope_id=tenant_id,
                    key=key,
                    value_json=json.dumps(value),
                    updated_by=str(user_id),
                )
            await session.commit()

        return validated

    # ── User settings ─────────────────────────────────────────────────────

    async def get_user(self, user_id: str) -> UserSettings:
        async with self._session_factory() as session:
            repo = SettingsRepository(session)
            rows = await repo.list_by_scope(_SCOPE_USER, user_id)
            stored = {r.key: json.loads(r.value_json) for r in rows}
            return UserSettings(**stored)

    async def set_user(self, user_id: str, data: dict) -> UserSettings:
        current = await self.get_user(user_id)
        merged = {**current.model_dump(), **data}
        validated = UserSettings(**merged)

        async with self._session_factory() as session:
            repo = SettingsRepository(session)
            for key, value in validated.model_dump().items():
                await repo.upsert(
                    scope=_SCOPE_USER,
                    scope_id=user_id,
                    key=key,
                    value_json=json.dumps(value),
                    updated_by=user_id,
                )
            await session.commit()

        return validated

    # ── Resolution helpers ────────────────────────────────────────────────

    async def resolve(
        self,
        key: str,
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> Any:
        """Walk user -> tenant -> global -> schema default for a single key."""
        if user_id is not None:
            async with self._session_factory() as session:
                repo = SettingsRepository(session)
                row = await repo.get(_SCOPE_USER, user_id, key)
                if row is not None:
                    return json.loads(row.value_json)

        if tenant_id is not None:
            async with self._session_factory() as session:
                repo = SettingsRepository(session)
                row = await repo.get(_SCOPE_TENANT, tenant_id, key)
                if row is not None:
                    return json.loads(row.value_json)

        async with self._session_factory() as session:
            repo = SettingsRepository(session)
            row = await repo.get(_SCOPE_GLOBAL, _GLOBAL_SCOPE_ID, key)
            if row is not None:
                return json.loads(row.value_json)

        global_defaults = GlobalSettings().model_dump()
        if key in global_defaults:
            return global_defaults[key]
        tenant_defaults = TenantSettings().model_dump()
        if key in tenant_defaults:
            return tenant_defaults[key]
        user_defaults = UserSettings().model_dump()
        if key in user_defaults:
            return user_defaults[key]

        return None

    async def resolve_all(
        self,
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> dict:
        """Return a merged dict: global defaults <- tenant overrides <- user overrides."""
        global_settings = await self.get_global()
        resolved: dict = global_settings.model_dump()

        if tenant_id is not None:
            tenant_settings = await self.get_tenant(tenant_id)
            for key, value in tenant_settings.model_dump().items():
                if value is not None:
                    resolved[key] = value

        if user_id is not None:
            user_settings = await self.get_user(user_id)
            for key, value in user_settings.model_dump().items():
                if value is not None:
                    resolved[key] = value

        return resolved

    async def get_effective(
        self,
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> EffectiveSettings:
        """Return the full structured effective settings view."""
        global_settings = await self.get_global()
        tenant_settings = (
            await self.get_tenant(tenant_id) if tenant_id is not None else None
        )
        user_settings = (
            await self.get_user(user_id) if user_id is not None else UserSettings()
        )
        resolved = await self.resolve_all(user_id=user_id, tenant_id=tenant_id)

        return EffectiveSettings(
            global_settings=global_settings,
            tenant_settings=tenant_settings,
            user_settings=user_settings,
            resolved=resolved,
        )
