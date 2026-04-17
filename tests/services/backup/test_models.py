"""Smoke tests for BackupSchedule and BackupRun ORM models.

Uses an in-memory SQLite engine + Base.metadata.create_all (not alembic) —
the alembic migration is exercised separately by the deploy pipeline.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scottycore.core.database import Base
from scottycore.services.backup.models import (
    KIND_DELTA,
    KIND_FULL,
    MANAGED_LOCAL,
    MANAGED_SCOTTYDEV,
    SCOPE_PLATFORM,
    SCOPE_TENANT,
    STATUS_SUCCESS,
    BackupRun,
    BackupSchedule,
)


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'backup.db'}"
    engine = create_async_engine(url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_schedule_insert_defaults(session_factory) -> None:
    async with session_factory() as s:
        sch = BackupSchedule(
            name="nightly",
            scope=SCOPE_PLATFORM,
            sink_type="local_disk",
            sink_config={"root_dir": "/app/data"},
            cron_expr="0 2 * * *",
        )
        s.add(sch)
        await s.commit()
        await s.refresh(sch)

        assert sch.id is not None
        assert sch.is_active is True
        assert sch.managed_by == MANAGED_LOCAL
        assert sch.kind == KIND_FULL
        assert sch.remote_id is None
        assert sch.passphrase_override_fingerprint is None


@pytest.mark.asyncio
async def test_schedule_scottydev_managed_has_unique_remote_id(session_factory) -> None:
    async with session_factory() as s:
        s.add(
            BackupSchedule(
                name="a",
                scope=SCOPE_PLATFORM,
                sink_type="scottydev",
                managed_by=MANAGED_SCOTTYDEV,
                remote_id="abc-123",
            )
        )
        s.add(
            BackupSchedule(
                name="b",
                scope=SCOPE_TENANT,
                tenant_slug="acme",
                sink_type="scottydev",
                managed_by=MANAGED_SCOTTYDEV,
                remote_id="abc-123",  # DUP
            )
        )
        with pytest.raises(IntegrityError):
            await s.commit()


@pytest.mark.asyncio
async def test_run_records_fingerprint_and_encryption(session_factory) -> None:
    async with session_factory() as s:
        run = BackupRun(
            app_slug="scottybiz",
            scope=SCOPE_PLATFORM,
            kind=KIND_DELTA,
            sink_type="local_disk",
            status=STATUS_SUCCESS,
            sink_locator="scottybiz/platform/20260416T100000Z-delta.tar.gz.gpg",
            bytes_written=4096,
            sha256="deadbeef" * 8,
            encrypted=True,
            key_fingerprint="abcd1234",
            started_at=datetime(2026, 4, 16, 10, 0, tzinfo=timezone.utc),
            finished_at=datetime(2026, 4, 16, 10, 2, tzinfo=timezone.utc),
        )
        s.add(run)
        await s.commit()
        await s.refresh(run)

        assert run.encrypted is True
        assert run.key_fingerprint == "abcd1234"
        assert run.managed_by == MANAGED_LOCAL


@pytest.mark.asyncio
async def test_query_schedules_by_managed_by(session_factory) -> None:
    async with session_factory() as s:
        s.add(BackupSchedule(name="l", scope=SCOPE_PLATFORM, sink_type="local_disk"))
        s.add(
            BackupSchedule(
                name="r",
                scope=SCOPE_PLATFORM,
                sink_type="scottydev",
                managed_by=MANAGED_SCOTTYDEV,
                remote_id="xyz",
            )
        )
        await s.commit()

        result = await s.scalars(
            select(BackupSchedule).where(BackupSchedule.managed_by == MANAGED_SCOTTYDEV)
        )
        rows = list(result)
        assert len(rows) == 1
        assert rows[0].remote_id == "xyz"
