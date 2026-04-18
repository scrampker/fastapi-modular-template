"""Tests for AutoUpdateService — pin detection, mode gating, bumping."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scottycore.core.database import Base
from scottycore.services.auto_update import AutoUpdateService, MODE_AUTO, MODE_NOTIFY, MODE_OFF
from scottycore.services.settings.repository import SettingsRepository


@pytest_asyncio.fixture
async def factory(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'au.db'}"
    engine = create_async_engine(url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield f
    finally:
        await engine.dispose()


def _pp(tmp_path: Path, pin: str) -> Path:
    pp = tmp_path / "pyproject.toml"
    pp.write_text(
        f'''[project]
name = "demo"
dependencies = [
    "scottycore @ git+https://github.com/scrampker/scottycore.git@{pin}",
]
''',
        encoding="utf-8",
    )
    return tmp_path


async def _set_mode(factory, mode: str) -> None:
    async with factory() as s:
        repo = SettingsRepository(s)
        await repo.upsert(
            scope="global",
            scope_id=None,
            key="scottycore.update.mode",
            value_json=json.dumps(mode),
            updated_by=None,
        )
        await s.commit()


def _make_service(root: Path, factory, handler) -> AutoUpdateService:
    """AutoUpdateService with a patched httpx transport."""
    svc = AutoUpdateService(
        repo_root=root,
        session_factory=factory,
        forge_api="http://fake.test/repos/x/y",
    )
    # Monkeypatch the fetcher to use the mock transport.
    async def _fake_fetch() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            r = await c.get("http://fake.test/repos/x/y/releases/latest")
            if r.status_code == 404:
                r2 = await c.get("http://fake.test/repos/x/y/tags")
                return r2.json()[0]["name"]
            return r.json()["tag_name"]

    svc._fetch_latest_tag = _fake_fetch  # type: ignore[assignment]
    return svc


@pytest.mark.asyncio
async def test_up_to_date_returns_none(tmp_path, factory) -> None:
    root = _pp(tmp_path, "v1.2.3")

    def handler(r):  # noqa: ARG001
        return httpx.Response(200, json={"tag_name": "v1.2.3"})

    svc = _make_service(root, factory, handler)
    result = await svc.check_once()
    assert result.action_taken == "none"
    assert result.current_pin == "v1.2.3"
    assert result.latest_pin == "v1.2.3"


@pytest.mark.asyncio
async def test_off_mode_does_nothing(tmp_path, factory) -> None:
    root = _pp(tmp_path, "v1.0.0")
    await _set_mode(factory, MODE_OFF)

    def handler(r):  # noqa: ARG001
        return httpx.Response(200, json={"tag_name": "v2.0.0"})

    svc = _make_service(root, factory, handler)
    result = await svc.check_once()
    assert result.mode == MODE_OFF
    assert result.action_taken == "none"
    # pyproject untouched
    assert "v1.0.0" in (root / "pyproject.toml").read_text()


@pytest.mark.asyncio
async def test_notify_mode_writes_pending_setting(tmp_path, factory) -> None:
    root = _pp(tmp_path, "v1.0.0")
    await _set_mode(factory, MODE_NOTIFY)

    def handler(r):  # noqa: ARG001
        return httpx.Response(200, json={"tag_name": "v1.0.1"})

    svc = _make_service(root, factory, handler)
    result = await svc.check_once()

    assert result.action_taken == "notified"
    assert "v1.0.0" in (root / "pyproject.toml").read_text()

    async with factory() as s:
        repo = SettingsRepository(s)
        pending = await repo.get("global", None, "scottycore.update.pending")
    assert pending is not None
    payload = json.loads(pending.value_json)
    assert payload["current"] == "v1.0.0"
    assert payload["latest"] == "v1.0.1"


@pytest.mark.asyncio
async def test_auto_mode_bumps_pin_and_drops_flag(tmp_path, factory) -> None:
    root = _pp(tmp_path, "v1.0.0")
    await _set_mode(factory, MODE_AUTO)

    def handler(r):  # noqa: ARG001
        return httpx.Response(200, json={"tag_name": "v1.0.1"})

    svc = _make_service(root, factory, handler)
    result = await svc.check_once()

    assert result.action_taken == "auto-updated"
    assert "v1.0.1" in (root / "pyproject.toml").read_text()
    assert "v1.0.0" not in (root / "pyproject.toml").read_text()
    assert (root / ".scottycore-auto-update-requested").is_file()


@pytest.mark.asyncio
async def test_missing_pyproject_reports_error(tmp_path, factory) -> None:
    svc = AutoUpdateService(
        repo_root=tmp_path,
        session_factory=factory,
    )
    result = await svc.check_once()
    assert result.action_taken == "error"


@pytest.mark.asyncio
async def test_malformed_pin_detects_no_pin(tmp_path, factory) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\ndependencies = ["fastapi"]\n', encoding="utf-8"
    )

    def handler(r):  # noqa: ARG001
        return httpx.Response(200, json={"tag_name": "v1.0.0"})

    svc = _make_service(tmp_path, factory, handler)
    result = await svc.check_once()
    assert result.action_taken == "error"
    assert "scottycore" in result.detail.lower()


@pytest.mark.asyncio
async def test_latest_falls_back_to_tags_on_404(tmp_path, factory) -> None:
    root = _pp(tmp_path, "v1.0.0")
    await _set_mode(factory, MODE_NOTIFY)

    def handler(r):
        if "releases/latest" in str(r.url):
            return httpx.Response(404)
        return httpx.Response(200, json=[{"name": "v1.5.0"}, {"name": "v1.4.0"}])

    svc = _make_service(root, factory, handler)
    result = await svc.check_once()
    assert result.action_taken == "notified"
    assert result.latest_pin == "v1.5.0"


@pytest.mark.asyncio
async def test_unknown_mode_falls_back_to_default_notify(tmp_path, factory) -> None:
    root = _pp(tmp_path, "v1.0.0")
    await _set_mode(factory, "invalid-value")

    def handler(r):  # noqa: ARG001
        return httpx.Response(200, json={"tag_name": "v1.0.1"})

    svc = _make_service(root, factory, handler)
    result = await svc.check_once()
    assert result.mode == MODE_NOTIFY
    assert result.action_taken == "notified"
