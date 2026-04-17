"""FilesContributor — export/restore covering platform + tenant scopes."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scottycore.core.database import Base
from scottycore.services.backup.files_contributor import FilesContributor
from scottycore.services.backup.schemas import BackupScope
from scottycore.services.tenants.models import Tenant


@pytest_asyncio.fixture
async def factory(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'files.db'}"
    engine = create_async_engine(url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield f
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_platform_scope_walks_full_tree(tmp_path, factory) -> None:
    base = tmp_path / "uploads"
    (base / "acme").mkdir(parents=True)
    (base / "acme" / "a.txt").write_bytes(b"one")
    (base / "globex").mkdir(parents=True)
    (base / "globex" / "b.txt").write_bytes(b"two")

    c = FilesContributor(base, factory)
    export = await c.export(BackupScope.PLATFORM, tenant_id=None)
    paths = {rel for rel, _ in export.files}
    assert paths == {"acme/a.txt", "globex/b.txt"}


@pytest.mark.asyncio
async def test_tenant_scope_requires_slug_and_limits_walk(tmp_path, factory) -> None:
    base = tmp_path / "uploads"
    (base / "acme").mkdir(parents=True)
    (base / "acme" / "a.txt").write_bytes(b"one")
    (base / "globex").mkdir(parents=True)
    (base / "globex" / "b.txt").write_bytes(b"two")

    async with factory() as s:
        t = Tenant(slug="acme", name="Acme")
        s.add(t)
        await s.commit()
        await s.refresh(t)
        tenant_id = str(t.id)

    c = FilesContributor(base, factory)
    export = await c.export(BackupScope.TENANT, tenant_id=tenant_id)
    paths = {rel for rel, _ in export.files}
    assert paths == {"acme/a.txt"}


@pytest.mark.asyncio
async def test_size_cap_skips_large_file(tmp_path, factory) -> None:
    base = tmp_path / "uploads"
    (base / "acme").mkdir(parents=True)
    small = base / "acme" / "small.txt"
    large = base / "acme" / "large.bin"
    small.write_bytes(b"ok")
    large.write_bytes(b"x" * 2048)

    c = FilesContributor(base, factory, max_bytes_per_file=1024)
    export = await c.export(BackupScope.PLATFORM, tenant_id=None)
    paths = {rel for rel, _ in export.files}
    assert "acme/small.txt" in paths
    assert "acme/large.bin" not in paths


@pytest.mark.asyncio
async def test_restore_writes_files_and_rejects_escape(tmp_path, factory) -> None:
    base = tmp_path / "uploads"
    base.mkdir()

    c = FilesContributor(base, factory)
    await c.restore(
        scope=BackupScope.PLATFORM,
        tenant_id=None,
        rows=[],
        files=[
            ("acme/a.txt", b"hello"),
            ("../escape.txt", b"should-not-land-here"),  # traversal attempt
        ],
        session_factory=factory,
    )
    assert (base / "acme" / "a.txt").read_bytes() == b"hello"
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.asyncio
async def test_tenant_scope_without_session_factory_returns_empty(tmp_path) -> None:
    base = tmp_path / "uploads"
    (base / "acme").mkdir(parents=True)
    (base / "acme" / "a.txt").write_bytes(b"x")

    c = FilesContributor(base, session_factory=None)
    export = await c.export(BackupScope.TENANT, tenant_id="some-uuid")
    assert export.files == []
