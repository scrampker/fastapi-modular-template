"""End-to-end CLI tests — export + restore round trip on a SQLite DB."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scottycore.services.backup.cli import (
    _build_sink,
    _default_download_path,
    _ops_user_id,
    _resolve_passphrase,
    main,
)


def test_ops_user_id_is_stable() -> None:
    assert _ops_user_id() == _ops_user_id()


def test_default_download_path_includes_scope_tag() -> None:
    p = _default_download_path("scottybiz", "platform", None)
    assert p.startswith("scottybiz-platform-")
    t = _default_download_path("scottybiz", "tenant", "acme")
    assert t.startswith("scottybiz-tenant-acme-")


def test_resolve_passphrase_passthrough() -> None:
    assert _resolve_passphrase("literal") == "literal"
    assert _resolve_passphrase(None) is None


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_build_sink_download() -> None:
    from scottycore.services.backup.sinks import DownloadSink

    sink = _build_sink(_Args(sink="download"))
    assert isinstance(sink, DownloadSink)


def test_build_sink_local_disk(tmp_path) -> None:
    from scottycore.services.backup.sinks import LocalDiskSink

    sink = _build_sink(_Args(sink="local_disk", root_dir=str(tmp_path)))
    assert isinstance(sink, LocalDiskSink)
    assert sink.root == tmp_path.resolve()


def test_build_sink_scottydev_requires_base_url() -> None:
    with pytest.raises(ValueError, match="base-url"):
        _build_sink(_Args(sink="scottydev", base_url=None, token=None))


def test_cli_help_returns_zero(capsys) -> None:
    rc = main([])
    assert rc == 2  # no subcommand → print help
    assert "scottycore-backup" in capsys.readouterr().out


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Point scottycore at a fresh SQLite DB for the CLI run."""
    db_path = tmp_path / "scotty.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("APP_NAME", "clitest")
    monkeypatch.setenv("APP_DEBUG", "false")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
    monkeypatch.setenv("ADMIN_EMAIL", "admin@test")

    # Build the schema so the CLI's ORM calls succeed.
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    from scottycore.core.database import Base

    async def _create():
        engine = create_async_engine(url, echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_create())

    return url


def test_cli_export_local_disk_round_trip(tmp_db, tmp_path, capsys, monkeypatch) -> None:
    root = tmp_path / "backups"
    rc = main(
        [
            "export",
            "--scope",
            "platform",
            "--sink",
            "local_disk",
            "--root-dir",
            str(root),
        ]
    )
    assert rc == 0, capsys.readouterr().out
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["status"] == "ok"
    assert out["sink"] == "local_disk"
    assert not out["encrypted"]

    bundle = root / out["locator"]
    assert bundle.is_file()

    rc2 = main(
        [
            "verify",
            "--sink",
            "local_disk",
            "--root-dir",
            str(root),
            "--locator",
            out["locator"],
            "--expected-sha256",
            out["sha256"],
        ]
    )
    assert rc2 == 0


def test_cli_export_with_passphrase_produces_gpg(tmp_db, tmp_path, capsys) -> None:
    root = tmp_path / "enc"
    rc = main(
        [
            "export",
            "--scope",
            "platform",
            "--sink",
            "local_disk",
            "--root-dir",
            str(root),
            "--passphrase",
            "hunter2",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["encrypted"] is True
    assert out["key_fingerprint"] is not None
    assert out["locator"].endswith(".tar.gz.gpg")


def test_cli_rotate_key(tmp_db, tmp_path, capsys) -> None:
    # First export encrypted so we have something to rotate.
    root = tmp_path / "enc"
    rc = main(
        [
            "export",
            "--scope",
            "platform",
            "--sink",
            "local_disk",
            "--root-dir",
            str(root),
            "--passphrase",
            "old-pass",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    src = root / out["locator"]
    dst = tmp_path / "rotated.tar.gz.gpg"

    rc2 = main(
        [
            "rotate-key",
            "--in",
            str(src),
            "--out",
            str(dst),
            "--old-passphrase",
            "old-pass",
            "--new-passphrase",
            "new-pass",
        ]
    )
    assert rc2 == 0
    rotated_out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rotated_out["old_fingerprint"] != rotated_out["new_fingerprint"]
    assert dst.is_file()


def test_cli_list_runs_empty(tmp_db, capsys) -> None:
    rc = main(["list-runs", "--limit", "5"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert json.loads(out) == []
