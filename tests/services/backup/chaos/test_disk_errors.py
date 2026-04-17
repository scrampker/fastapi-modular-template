"""Chaos tests — disk/permission failures on LocalDiskSink.

Probes:
- get() on non-existent locator
- delete() on non-existent locator
- write to a read-only directory (simulated via chmod)
- zero-byte blob put and get
- very large blob (stress allocation)
- locator with path components that don't exist
"""

from __future__ import annotations

import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scottycore.services.backup.sinks.base import BackupBlob, SinkNotFoundError
from scottycore.services.backup.sinks.local_disk import LocalDiskSink


def _blob(data: bytes = b"data", ts: datetime | None = None) -> BackupBlob:
    return BackupBlob(
        data=data,
        sha256="aa" * 32,
        size=len(data),
        app_slug="app",
        scope="platform",
        kind="full",
        created_at=ts or datetime(2024, 1, 1, tzinfo=timezone.utc),
        encrypted=False,
    )


@pytest.fixture()
def sink(tmp_path: Path) -> LocalDiskSink:
    return LocalDiskSink(tmp_path / "sink")


# ── Missing file ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_missing_locator_raises_sink_not_found(sink: LocalDiskSink) -> None:
    with pytest.raises(SinkNotFoundError):
        await sink.get("app/platform/20240101T000000Z-full.tar.gz")


@pytest.mark.asyncio
async def test_delete_missing_locator_raises_sink_not_found(sink: LocalDiskSink) -> None:
    with pytest.raises(SinkNotFoundError):
        await sink.delete("app/platform/20240101T000000Z-full.tar.gz")


@pytest.mark.asyncio
async def test_verify_missing_locator_raises_sink_not_found(sink: LocalDiskSink) -> None:
    with pytest.raises(SinkNotFoundError):
        await sink.verify("nonexistent", "deadbeef")


# ── Read-only directory ────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.skipif(os.getuid() == 0, reason="root bypasses filesystem permissions")
async def test_put_into_readonly_dir_raises(tmp_path: Path) -> None:
    """If the sink root is read-only, put() must raise (not silently succeed)."""
    root = tmp_path / "readonly_sink"
    root.mkdir()
    sink = LocalDiskSink(root)

    # Make the directory read-only
    root.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.raises((OSError, PermissionError)):
            await sink.put(_blob(b"payload"))
    finally:
        root.chmod(stat.S_IRWXU)  # restore so pytest can clean up


# ── Zero-byte blob ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_zero_byte_blob_round_trips(sink: LocalDiskSink) -> None:
    result = await sink.put(_blob(b""))
    stored = await sink.get(result.locator)
    assert stored == b""


# ── Large blob ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_large_blob_round_trips(sink: LocalDiskSink) -> None:
    """10 MB blob to exercise the threaded write path."""
    payload = b"X" * (10 * 1024 * 1024)
    result = await sink.put(_blob(payload))
    stored = await sink.get(result.locator)
    assert stored == payload
    assert result.bytes_written == len(payload)


# ── list_snapshots on empty/broken sidecars ────────────────────────────────


@pytest.mark.asyncio
async def test_list_snapshots_empty_dir(sink: LocalDiskSink) -> None:
    entries = await sink.list_snapshots()
    assert entries == []


@pytest.mark.asyncio
async def test_list_snapshots_with_corrupt_sidecar(
    sink: LocalDiskSink, tmp_path: Path
) -> None:
    """Corrupt .meta.json file should be silently skipped, not crash."""
    # Put a valid blob first to create the directory structure
    result = await sink.put(_blob())

    # Now corrupt its sidecar
    sidecar = (sink.root / result.locator).with_suffix(
        (sink.root / result.locator).suffix + ".meta.json"
    )
    sidecar.write_bytes(b"NOT JSON {{{")

    # list_snapshots must not raise
    entries = await sink.list_snapshots()
    # The corrupt entry should be skipped
    assert all(e.locator != result.locator for e in entries)


@pytest.mark.asyncio
async def test_list_snapshots_sidecar_without_bundle(
    sink: LocalDiskSink, tmp_path: Path
) -> None:
    """A .meta.json without a matching bundle file should be skipped."""
    import json
    from datetime import timezone

    orphan_sidecar = sink.root / "orphan.meta.json"
    orphan_sidecar.parent.mkdir(parents=True, exist_ok=True)
    orphan_sidecar.write_text(
        json.dumps(
            {
                "sha256": "aa" * 32,
                "size": 0,
                "app_slug": "app",
                "scope": "platform",
                "kind": "full",
                "encrypted": False,
                "key_fingerprint": None,
                "tenant_slug": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )
    entries = await sink.list_snapshots()
    assert not any(e.locator == "orphan" for e in entries)
