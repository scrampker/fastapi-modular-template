"""BackupService wiring: built-ins auto-register in FK-safe order."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scottycore.core.database import Base
from scottycore.services.audit.service import AuditService
from scottycore.services.backup.schemas import BackupScope
from scottycore.services.backup.wiring import build_backup_service


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'w.db'}"
    engine = create_async_engine(url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_built_in_contributors_registered_in_fk_order(session_factory) -> None:
    audit = AuditService(session_factory)
    svc = build_backup_service(
        session_factory, audit, app_name="test", app_version="0.0.0"
    )

    ids = [c.id for c in svc.list_contributors()]
    # Tenants must precede users (FK), settings/items must follow users.
    assert ids.index("tenants") < ids.index("users")
    assert ids.index("users") < ids.index("settings")
    assert ids.index("users") < ids.index("items")
    assert {"tenants", "users", "settings", "items", "audit_log"}.issubset(ids)


@pytest.mark.asyncio
async def test_platform_scope_excludes_tenant_only(session_factory) -> None:
    audit = AuditService(session_factory)
    svc = build_backup_service(
        session_factory, audit, app_name="test", app_version="0.0.0"
    )
    platform_ids = {c.id for c in svc.list_contributors(BackupScope.PLATFORM)}
    assert "users" in platform_ids
    assert "tenants" in platform_ids


@pytest.mark.asyncio
async def test_consumer_can_register_additional_contributor(session_factory) -> None:
    audit = AuditService(session_factory)
    svc = build_backup_service(
        session_factory, audit, app_name="test", app_version="0.0.0"
    )

    class DomainContributor:
        contributor_id = "widgets"
        scopes = {BackupScope.TENANT}
        description = "Widgets for the app's own domain."

        async def export(self, scope, tenant_id):
            from scottycore.services.backup.schemas import ContributorExport

            return ContributorExport()

        async def restore(self, scope, tenant_id, rows, files, session_factory):
            return 0

    svc.register(DomainContributor())
    ids = [c.id for c in svc.list_contributors()]
    assert "widgets" in ids
    assert ids.index("widgets") > ids.index("items")  # appended after built-ins
