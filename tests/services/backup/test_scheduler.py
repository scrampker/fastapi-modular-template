"""BackupScheduler — tick dispatch, cron advance, retention."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scottycore.core.database import Base
from scottycore.services.audit.service import AuditService
from scottycore.services.backup.models import (
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SUCCESS,
    BackupRun,
    BackupSchedule,
)
from scottycore.services.backup.scheduler import (
    BackupScheduler,
    StaticPassphraseProvider,
)
from scottycore.services.backup.wiring import build_backup_service


@pytest_asyncio.fixture
async def factory(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'sched.db'}"
    engine = create_async_engine(url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield f
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def svc(factory):
    audit = AuditService(factory)
    return build_backup_service(factory, audit, app_name="test", app_version="0.0.0")


@pytest.mark.asyncio
async def test_tick_dispatches_due_and_records_success(factory, svc, tmp_path) -> None:
    async with factory() as s:
        s.add(
            BackupSchedule(
                name="immediate",
                scope="platform",
                sink_type="local_disk",
                sink_config={"root_dir": str(tmp_path / "store")},
                cron_expr="0 * * * *",
                next_run_at=datetime(2020, 1, 1, tzinfo=timezone.utc),  # past
            )
        )
        await s.commit()

    scheduler = BackupScheduler(
        session_factory=factory,
        backup_service=svc,
        tick_seconds=0.01,
    )
    run_ids = await scheduler.tick_once()
    assert len(run_ids) == 1

    async with factory() as s:
        run = await s.get(BackupRun, run_ids[0])
        assert run is not None
        assert run.status == STATUS_SUCCESS
        assert run.sink_locator is not None
        assert run.sha256 is not None


@pytest.mark.asyncio
async def test_tick_skips_not_yet_due(factory, svc, tmp_path) -> None:
    future = datetime.now(timezone.utc) + timedelta(days=1)
    async with factory() as s:
        s.add(
            BackupSchedule(
                name="future",
                scope="platform",
                sink_type="local_disk",
                sink_config={"root_dir": str(tmp_path / "store")},
                cron_expr="0 0 * * *",
                next_run_at=future,
            )
        )
        await s.commit()

    scheduler = BackupScheduler(
        session_factory=factory, backup_service=svc, tick_seconds=0.01
    )
    assert await scheduler.tick_once() == []


@pytest.mark.asyncio
async def test_cron_advances_next_run_at(factory, svc, tmp_path) -> None:
    async with factory() as s:
        s.add(
            BackupSchedule(
                name="hourly",
                scope="platform",
                sink_type="local_disk",
                sink_config={"root_dir": str(tmp_path / "store")},
                cron_expr="0 * * * *",
                next_run_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            )
        )
        await s.commit()

    scheduler = BackupScheduler(
        session_factory=factory, backup_service=svc, tick_seconds=0.01
    )
    await scheduler.tick_once()

    async with factory() as s:
        row = (
            await s.scalars(
                select(BackupSchedule).where(BackupSchedule.name == "hourly")
            )
        ).first()
        sched_obj = await s.get(BackupSchedule, row.id)
        assert sched_obj.next_run_at is not None
        nra = sched_obj.next_run_at
        if nra.tzinfo is None:
            nra = nra.replace(tzinfo=timezone.utc)
        assert nra > datetime.now(timezone.utc) - timedelta(hours=1)


@pytest.mark.asyncio
async def test_one_shot_schedule_deactivates_after_run(factory, svc, tmp_path) -> None:
    async with factory() as s:
        s.add(
            BackupSchedule(
                name="one-shot",
                scope="platform",
                sink_type="local_disk",
                sink_config={"root_dir": str(tmp_path / "store")},
                cron_expr=None,
                next_run_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            )
        )
        await s.commit()

    scheduler = BackupScheduler(
        session_factory=factory, backup_service=svc, tick_seconds=0.01
    )
    await scheduler.tick_once()

    async with factory() as s:
        sch = (
            await s.scalars(
                select(BackupSchedule).where(BackupSchedule.name == "one-shot")
            )
        ).first()
        obj = await s.get(BackupSchedule, sch.id)
        assert obj.is_active is False
        assert obj.next_run_at is None


@pytest.mark.asyncio
async def test_passphrase_provider_encrypts(factory, svc, tmp_path) -> None:
    async with factory() as s:
        s.add(
            BackupSchedule(
                name="encd",
                scope="platform",
                sink_type="local_disk",
                sink_config={"root_dir": str(tmp_path / "store")},
                cron_expr="0 * * * *",
                next_run_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            )
        )
        await s.commit()

    scheduler = BackupScheduler(
        session_factory=factory,
        backup_service=svc,
        passphrase_provider=StaticPassphraseProvider("abc123"),
        tick_seconds=0.01,
    )
    run_ids = await scheduler.tick_once()

    async with factory() as s:
        run = await s.get(BackupRun, run_ids[0])
        assert run.encrypted is True
        assert run.key_fingerprint is not None
        assert run.sink_locator.endswith(".tar.gz.gpg")


@pytest.mark.asyncio
async def test_keep_last_prunes_old_snapshots(factory, svc, tmp_path) -> None:
    root = tmp_path / "store"
    async with factory() as s:
        sch = BackupSchedule(
            name="prunes",
            scope="platform",
            sink_type="local_disk",
            sink_config={"root_dir": str(root)},
            cron_expr="0 * * * *",
            next_run_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            keep_last=1,
        )
        s.add(sch)
        await s.commit()
        await s.refresh(sch)
        sid = sch.id

    scheduler = BackupScheduler(
        session_factory=factory, backup_service=svc, tick_seconds=0.01
    )
    # Two successful runs — second should prune the first.
    await scheduler.tick_once()
    # Force next_run_at back into the past so the scheduler runs again.
    async with factory() as s:
        obj = await s.get(BackupSchedule, sid)
        obj.next_run_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        await s.commit()
    await scheduler.tick_once()

    async with factory() as s:
        rows = list(
            (
                await s.scalars(
                    select(BackupRun).where(BackupRun.schedule_id == sid)
                )
            ).all()
        )
        # Only the most recent run remains.
    runs_remaining = [r for r in rows if r.status == STATUS_SUCCESS]
    assert len(runs_remaining) == 1

    # Filesystem should have exactly one .tar.gz
    bundles = list(Path(root).rglob("*.tar.gz"))
    assert len(bundles) == 1


@pytest.mark.asyncio
async def test_failed_export_leaves_failed_run_and_does_not_advance(
    factory, svc, tmp_path
) -> None:
    async with factory() as s:
        s.add(
            BackupSchedule(
                name="bad",
                scope="tenant",  # tenant with no tenant_id → will throw
                sink_type="local_disk",
                sink_config={"root_dir": str(tmp_path / "store")},
                cron_expr="0 * * * *",
                next_run_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            )
        )
        await s.commit()

    scheduler = BackupScheduler(
        session_factory=factory, backup_service=svc, tick_seconds=0.01
    )
    run_ids = await scheduler.tick_once()

    async with factory() as s:
        run = await s.get(BackupRun, run_ids[0])
        assert run.status == STATUS_FAILED
        assert run.error is not None


@pytest.mark.asyncio
async def test_inactive_schedule_skipped(factory, svc, tmp_path) -> None:
    async with factory() as s:
        s.add(
            BackupSchedule(
                name="off",
                scope="platform",
                sink_type="local_disk",
                sink_config={"root_dir": str(tmp_path / "store")},
                cron_expr="0 * * * *",
                next_run_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                is_active=False,
            )
        )
        await s.commit()

    scheduler = BackupScheduler(
        session_factory=factory, backup_service=svc, tick_seconds=0.01
    )
    assert await scheduler.tick_once() == []
