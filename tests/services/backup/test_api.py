"""Backup REST API — auth-gate and sink-builder tests.

Full round-trip tests require a live ServiceRegistry on ``app.state`` which
conftest.py doesn't currently wire up; they'll be added when the main app
fixture grows that capability. For now we verify:
  * routes are mounted under /api/v1/backups/*
  * unauthenticated callers are rejected
  * _build_sink rejects bad types and builds the right class
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from scottycore.api.v1.backup import _build_sink
from scottycore.core.auth import get_current_user
from scottycore.main import app
from scottycore.services.backup.sinks import (
    DownloadSink,
    LocalDiskSink,
    ScottyDevSink,
)


@pytest.fixture(autouse=True)
def _anon_with_stub_registry(tmp_path):
    """Stub get_current_user → anonymous, and attach a minimal ServiceRegistry
    to ``app.state`` so dependency resolution doesn't blow up before the auth
    gate triggers.
    """
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from scottycore.core.database import Base
    from scottycore.core.service_registry import ServiceRegistry

    url = f"sqlite+aiosqlite:///{tmp_path / 'api.db'}"
    engine = create_async_engine(url, echo=False)

    import asyncio

    async def _prep():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.get_event_loop().run_until_complete(_prep())
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app.state.registry = ServiceRegistry(factory)

    app.dependency_overrides[get_current_user] = lambda: None
    yield
    app.dependency_overrides.pop(get_current_user, None)
    try:
        del app.state.registry
    except AttributeError:
        pass


def test_build_sink_download() -> None:
    assert isinstance(_build_sink("download", {}), DownloadSink)


def test_build_sink_local_disk_with_root(tmp_path) -> None:
    sink = _build_sink("local_disk", {"root_dir": str(tmp_path)})
    assert isinstance(sink, LocalDiskSink)
    assert sink.root == tmp_path.resolve()


def test_build_sink_scottydev_requires_base_url() -> None:
    with pytest.raises(HTTPException) as exc:
        _build_sink("scottydev", {})
    assert exc.value.status_code == 400


def test_build_sink_scottydev_ok() -> None:
    sink = _build_sink("scottydev", {"base_url": "http://x", "token": "t"})
    assert isinstance(sink, ScottyDevSink)


def test_build_sink_unknown_type_rejected() -> None:
    with pytest.raises(HTTPException) as exc:
        _build_sink("tape-drive", {})
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_contributors_requires_auth() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        resp = await ac.get("/api/v1/backups/contributors")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_export_requires_auth() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        resp = await ac.post("/api/v1/backups/export", json={"scope": "platform"})
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_schedules_list_requires_auth() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        resp = await ac.get("/api/v1/backups/schedules")
    assert resp.status_code in (401, 403)
