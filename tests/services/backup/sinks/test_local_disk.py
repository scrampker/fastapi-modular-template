"""LocalDiskSink unit tests."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scottycore.services.backup.sinks import (
    BackupBlob,
    LocalDiskSink,
    SinkNotFoundError,
)


def _blob(
    data: bytes,
    *,
    app_slug: str = "demo",
    scope: str = "platform",
    kind: str = "full",
    encrypted: bool = False,
    tenant_slug: str | None = None,
    ts: datetime | None = None,
) -> BackupBlob:
    return BackupBlob(
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        app_slug=app_slug,
        scope=scope,
        kind=kind,
        created_at=ts or datetime(2026, 4, 16, 12, 0, tzinfo=timezone.utc),
        encrypted=encrypted,
        key_fingerprint="deadbeef" if encrypted else None,
        tenant_slug=tenant_slug,
    )


@pytest.mark.asyncio
async def test_put_writes_bundle_and_sidecar(tmp_path: Path) -> None:
    sink = LocalDiskSink(tmp_path)
    blob = _blob(b"hello world")

    result = await sink.put(blob)

    assert result.sink_type == "local_disk"
    assert result.bytes_written == len(b"hello world")

    target = tmp_path / result.locator
    assert target.is_file()
    assert target.read_bytes() == b"hello world"

    sidecar = target.with_suffix(target.suffix + ".meta.json")
    assert sidecar.is_file()


@pytest.mark.asyncio
async def test_put_is_atomic_via_rename(tmp_path: Path) -> None:
    sink = LocalDiskSink(tmp_path)
    blob = _blob(b"x" * 1024)

    await sink.put(blob)

    # No .part remnants after a clean write.
    assert not any(tmp_path.rglob("*.part"))


@pytest.mark.asyncio
async def test_get_round_trip(tmp_path: Path) -> None:
    sink = LocalDiskSink(tmp_path)
    payload = b"round-trip payload"
    result = await sink.put(_blob(payload))

    got = await sink.get(result.locator)
    assert got == payload


@pytest.mark.asyncio
async def test_get_rejects_path_traversal(tmp_path: Path) -> None:
    sink = LocalDiskSink(tmp_path)
    with pytest.raises(SinkNotFoundError):
        await sink.get("../outside.tar.gz")


@pytest.mark.asyncio
async def test_list_snapshots_filters(tmp_path: Path) -> None:
    sink = LocalDiskSink(tmp_path)
    await sink.put(_blob(b"a", app_slug="demo", scope="platform"))
    await sink.put(
        _blob(b"b", app_slug="demo", scope="tenant", tenant_slug="acme"),
    )
    await sink.put(_blob(b"c", app_slug="other", scope="platform"))

    all_snaps = await sink.list_snapshots()
    assert len(all_snaps) == 3

    demo_only = await sink.list_snapshots(app_slug="demo")
    assert {s.app_slug for s in demo_only} == {"demo"}
    assert len(demo_only) == 2

    tenant_only = await sink.list_snapshots(tenant_slug="acme")
    assert len(tenant_only) == 1
    assert tenant_only[0].tenant_slug == "acme"


@pytest.mark.asyncio
async def test_list_sorted_newest_first(tmp_path: Path) -> None:
    sink = LocalDiskSink(tmp_path)
    t1 = datetime(2026, 4, 16, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 16, 12, 0, tzinfo=timezone.utc)
    await sink.put(_blob(b"old", ts=t1))
    await sink.put(_blob(b"new", ts=t2))

    snaps = await sink.list_snapshots()
    assert snaps[0].created_at == t2
    assert snaps[1].created_at == t1


@pytest.mark.asyncio
async def test_delete_removes_bundle_and_sidecar(tmp_path: Path) -> None:
    sink = LocalDiskSink(tmp_path)
    result = await sink.put(_blob(b"bye"))
    target = tmp_path / result.locator
    sidecar = target.with_suffix(target.suffix + ".meta.json")
    assert target.exists() and sidecar.exists()

    await sink.delete(result.locator)

    assert not target.exists()
    assert not sidecar.exists()


@pytest.mark.asyncio
async def test_delete_missing_raises(tmp_path: Path) -> None:
    sink = LocalDiskSink(tmp_path)
    with pytest.raises(SinkNotFoundError):
        await sink.delete("nope/nothing.tar.gz")


@pytest.mark.asyncio
async def test_verify_detects_corruption(tmp_path: Path) -> None:
    sink = LocalDiskSink(tmp_path)
    blob = _blob(b"clean")
    result = await sink.put(blob)

    assert await sink.verify(result.locator, blob.sha256) is True

    # Corrupt the stored file.
    target = tmp_path / result.locator
    target.write_bytes(b"tampered")
    assert await sink.verify(result.locator, blob.sha256) is False


@pytest.mark.asyncio
async def test_encrypted_filename_has_gpg_suffix(tmp_path: Path) -> None:
    sink = LocalDiskSink(tmp_path)
    result = await sink.put(_blob(b"enc", encrypted=True))
    assert result.locator.endswith(".tar.gz.gpg")


@pytest.mark.asyncio
async def test_tenant_scope_goes_under_tenant_dir(tmp_path: Path) -> None:
    sink = LocalDiskSink(tmp_path)
    result = await sink.put(
        _blob(b"t", app_slug="demo", scope="tenant", tenant_slug="acme")
    )
    assert "demo/tenant/acme/" in result.locator
