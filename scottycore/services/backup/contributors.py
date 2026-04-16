"""Built-in scottycore backup contributors.

Each contributor dumps a slice of scottycore-owned data and can restore it.
All contributors use direct SQLAlchemy selects rather than going through the
service layer to avoid business-logic side-effects (e.g. audit re-writes)
during restore.

Restore semantics: "merge" (insert-or-update by PK).
Future extension: "replace" mode (delete all rows for scope, then insert)
should be implemented as a ``mode`` parameter on ``restore()``.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scottycore.services.backup.contributor import BackupContributor
from scottycore.services.backup.schemas import BackupScope, ContributorExport

# NOTE: export_delta implementations below use a simple updated_at >= since
# SQL filter.  The exclude_origins filter is intentionally omitted here;
# the MeshSyncEngine applies an in-Python pass using SyncRowOrigin lookups
# after the contributor returns rows.  Pushing this into SQL would require
# a JOIN on sync_row_origin which is not always available (e.g. if the sync
# tables haven't been created yet in a cold-start scenario).


# ── Serialisation helpers ──────────────────────────────────────────────────


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a SQLAlchemy ORM row to a JSON-safe plain dict.

    UUIDs and datetimes are stored as strings inside the models (String(36)
    and DateTime columns), so most values come out as native Python types that
    json.dumps handles directly.  We still normalise datetime objects to
    ISO-8601 for safety.
    """
    d: dict[str, Any] = {}
    for col in row.__table__.columns:
        val = getattr(row, col.name)
        if isinstance(val, datetime):
            val = val.isoformat()
        d[col.name] = val
    return d


def _hydrate_row(model_cls: Any, row: dict[str, Any]) -> dict[str, Any]:
    """Re-hydrate a serialised row for insertion.

    Serialisation flattens datetimes to ISO-8601 strings so JSON can carry them.
    SQLite's DateTime binding rejects strings, so on the way back in we parse
    ISO strings back to ``datetime`` for any DateTime column.
    """
    from sqlalchemy import DateTime
    hydrated: dict[str, Any] = {}
    for col in model_cls.__table__.columns:
        if col.name not in row:
            continue
        val = row[col.name]
        if val is not None and isinstance(col.type, DateTime) and isinstance(val, str):
            val = datetime.fromisoformat(val.replace("Z", "+00:00"))
        hydrated[col.name] = val
    return hydrated


async def _upsert_rows(
    session: AsyncSession,
    model_cls: Any,
    rows: list[dict[str, Any]],
) -> int:
    """Insert-or-update rows using SQLAlchemy's session.merge().

    merge() matches on primary key: it issues an UPDATE if the PK exists,
    INSERT otherwise.  FK violations are possible if contributors are restored
    out of order — callers must register contributors in dependency order.
    """
    count = 0
    for row in rows:
        instance = model_cls(**_hydrate_row(model_cls, row))
        await session.merge(instance)
        count += 1
    return count


# ── Contributors ───────────────────────────────────────────────────────────


class UsersContributor:
    """PLATFORM scope — dumps all users and user_tenant_roles."""

    contributor_id = "users"
    scopes: set[BackupScope] = {BackupScope.PLATFORM}
    description = "All users and their tenant role assignments (platform scope)."

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_row(self, contributor_id: str, row_id: str) -> dict[str, Any] | None:
        """Fetch a single user row by ID for LWW comparison."""
        from scottycore.services.users.models import User

        async with self._session_factory() as session:
            row = await session.get(User, row_id)
        if row is None:
            return None
        return {"_table": "users", **_row_to_dict(row)}

    async def export(
        self, scope: BackupScope, tenant_id: str | None
    ) -> ContributorExport:
        from scottycore.services.users.models import User, UserTenantRole

        async with self._session_factory() as session:
            users = list((await session.scalars(select(User))).all())
            utrs = list((await session.scalars(select(UserTenantRole))).all())

        rows: list[dict[str, Any]] = []
        rows.extend(_row_to_dict(u) for u in users)
        # Tag rows with a discriminator so restore can distinguish them.
        user_rows = [{"_table": "users", **_row_to_dict(u)} for u in users]
        utr_rows = [{"_table": "user_tenant_roles", **_row_to_dict(r)} for r in utrs]
        return ContributorExport(rows=user_rows + utr_rows)

    async def restore(
        self,
        scope: BackupScope,
        tenant_id: str | None,
        rows: list[dict[str, Any]],
        files: list[tuple[str, bytes]],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> int:
        from scottycore.services.users.models import User, UserTenantRole

        user_rows = [_strip_tag(r) for r in rows if r.get("_table") == "users"]
        utr_rows = [_strip_tag(r) for r in rows if r.get("_table") == "user_tenant_roles"]

        total = 0
        async with session_factory() as session:
            # Users first — UTRs reference users by FK
            total += await _upsert_rows(session, User, user_rows)
            total += await _upsert_rows(session, UserTenantRole, utr_rows)
            await session.commit()
        return total


class TenantsContributor:
    """PLATFORM scope — dumps tenants and roles."""

    contributor_id = "tenants"
    scopes: set[BackupScope] = {BackupScope.PLATFORM}
    description = "All tenants and role definitions (platform scope)."

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_row(self, contributor_id: str, row_id: str) -> dict[str, Any] | None:
        """Fetch a single tenant row by ID for LWW comparison."""
        from scottycore.services.tenants.models import Tenant

        async with self._session_factory() as session:
            row = await session.get(Tenant, row_id)
        if row is None:
            return None
        return {"_table": "tenants", **_row_to_dict(row)}

    async def export(
        self, scope: BackupScope, tenant_id: str | None
    ) -> ContributorExport:
        from scottycore.services.tenants.models import Tenant
        from scottycore.services.users.models import Role

        async with self._session_factory() as session:
            tenants = list((await session.scalars(select(Tenant))).all())
            roles = list((await session.scalars(select(Role))).all())

        tenant_rows = [{"_table": "tenants", **_row_to_dict(t)} for t in tenants]
        role_rows = [{"_table": "roles", **_row_to_dict(r)} for r in roles]
        return ContributorExport(rows=tenant_rows + role_rows)

    async def restore(
        self,
        scope: BackupScope,
        tenant_id: str | None,
        rows: list[dict[str, Any]],
        files: list[tuple[str, bytes]],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> int:
        from scottycore.services.tenants.models import Tenant
        from scottycore.services.users.models import Role

        tenant_rows = [_strip_tag(r) for r in rows if r.get("_table") == "tenants"]
        role_rows = [_strip_tag(r) for r in rows if r.get("_table") == "roles"]

        total = 0
        async with session_factory() as session:
            # Roles have no FKs so order doesn't matter between the two.
            total += await _upsert_rows(session, Role, role_rows)
            total += await _upsert_rows(session, Tenant, tenant_rows)
            await session.commit()
        return total


class SettingsContributor:
    """BOTH scopes — global settings at PLATFORM, tenant settings at TENANT."""

    contributor_id = "settings"
    scopes: set[BackupScope] = {BackupScope.PLATFORM, BackupScope.TENANT}
    description = "Global settings (platform) or per-tenant settings (tenant scope)."

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_row(self, contributor_id: str, row_id: str) -> dict[str, Any] | None:
        """Fetch a single settings row by ID for LWW comparison."""
        from scottycore.services.settings.models import Setting

        async with self._session_factory() as session:
            row = await session.get(Setting, row_id)
        if row is None:
            return None
        return _row_to_dict(row)

    async def export(
        self, scope: BackupScope, tenant_id: str | None
    ) -> ContributorExport:
        from scottycore.services.settings.models import Setting

        async with self._session_factory() as session:
            if scope == BackupScope.PLATFORM:
                rows_q = await session.scalars(
                    select(Setting).where(Setting.scope == "global")
                )
            else:
                rows_q = await session.scalars(
                    select(Setting).where(
                        Setting.scope == "tenant",
                        Setting.scope_id == tenant_id,
                    )
                )
            settings = list(rows_q.all())

        return ContributorExport(rows=[_row_to_dict(s) for s in settings])

    async def restore(
        self,
        scope: BackupScope,
        tenant_id: str | None,
        rows: list[dict[str, Any]],
        files: list[tuple[str, bytes]],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> int:
        from scottycore.services.settings.models import Setting

        async with session_factory() as session:
            count = await _upsert_rows(session, Setting, rows)
            await session.commit()
        return count


class ItemsContributor:
    """TENANT scope — dumps all items for the target tenant."""

    contributor_id = "items"
    scopes: set[BackupScope] = {BackupScope.TENANT}
    description = "All items for the target tenant."

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def export(
        self, scope: BackupScope, tenant_id: str | None
    ) -> ContributorExport:
        from scottycore.services.items.models import Item

        async with self._session_factory() as session:
            items = list(
                (await session.scalars(
                    select(Item).where(Item.tenant_id == tenant_id)
                )).all()
            )

        return ContributorExport(rows=[_row_to_dict(i) for i in items])

    async def export_delta(
        self,
        scope: BackupScope,
        tenant_id: str | None,
        since: datetime | None,
        exclude_origins: list[str],
    ) -> ContributorExport:
        """Delta export: only rows with updated_at >= since."""
        from scottycore.services.items.models import Item

        async with self._session_factory() as session:
            q = select(Item).where(Item.tenant_id == tenant_id)
            if since is not None:
                q = q.where(Item.updated_at >= since)
            items = list((await session.scalars(q)).all())

        return ContributorExport(rows=[_row_to_dict(i) for i in items])

    async def get_row(self, contributor_id: str, row_id: str) -> dict[str, Any] | None:
        """Fetch a single item row by ID for LWW comparison."""
        from scottycore.services.items.models import Item

        async with self._session_factory() as session:
            row = await session.get(Item, row_id)
        if row is None:
            return None
        return _row_to_dict(row)

    async def restore(
        self,
        scope: BackupScope,
        tenant_id: str | None,
        rows: list[dict[str, Any]],
        files: list[tuple[str, bytes]],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> int:
        from scottycore.services.items.models import Item

        async with session_factory() as session:
            count = await _upsert_rows(session, Item, rows)
            await session.commit()
        return count


class AuditLogContributor:
    """BOTH scopes — full audit log at PLATFORM; tenant-filtered at TENANT."""

    contributor_id = "audit_log"
    scopes: set[BackupScope] = {BackupScope.PLATFORM, BackupScope.TENANT}
    description = "Audit log entries (all at platform scope; tenant-filtered at tenant scope)."

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def export(
        self, scope: BackupScope, tenant_id: str | None
    ) -> ContributorExport:
        from scottycore.services.audit.models import AuditLog

        async with self._session_factory() as session:
            q = select(AuditLog)
            if scope == BackupScope.TENANT:
                q = q.where(AuditLog.tenant_id == tenant_id)
            rows = list((await session.scalars(q)).all())

        return ContributorExport(rows=[_row_to_dict(r) for r in rows])

    async def export_delta(
        self,
        scope: BackupScope,
        tenant_id: str | None,
        since: datetime | None,
        exclude_origins: list[str],
    ) -> ContributorExport:
        """Delta export: audit log rows created after ``since``.

        AuditLog rows are append-only (no updated_at), so we filter on
        created_at instead.  They also never need echo prevention since they
        are locally generated events.
        """
        from scottycore.services.audit.models import AuditLog

        async with self._session_factory() as session:
            q = select(AuditLog)
            if scope == BackupScope.TENANT:
                q = q.where(AuditLog.tenant_id == tenant_id)
            if since is not None:
                q = q.where(AuditLog.created_at >= since)
            rows = list((await session.scalars(q)).all())

        return ContributorExport(rows=[_row_to_dict(r) for r in rows])

    async def restore(
        self,
        scope: BackupScope,
        tenant_id: str | None,
        rows: list[dict[str, Any]],
        files: list[tuple[str, bytes]],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> int:
        from scottycore.services.audit.models import AuditLog

        async with session_factory() as session:
            count = await _upsert_rows(session, AuditLog, rows)
            await session.commit()
        return count


# ── Utility ────────────────────────────────────────────────────────────────


def _strip_tag(row: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *row* without the internal ``_table`` discriminator key."""
    return {k: v for k, v in row.items() if k != "_table"}
