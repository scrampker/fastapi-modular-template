"""DownloadSink unit tests."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from scottycore.services.backup.sinks import BackupBlob, DownloadSink, SinkError


def _blob(data: bytes) -> BackupBlob:
    return BackupBlob(
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        app_slug="demo",
        scope="platform",
        kind="full",
        created_at=datetime(2026, 4, 16, 12, 0, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_put_captures_blob_for_streaming() -> None:
    sink = DownloadSink()
    blob = _blob(b"payload")

    result = await sink.put(blob)

    assert result.sink_type == "download"
    assert sink.last is blob
    assert result.locator.endswith(".tar.gz")


@pytest.mark.asyncio
async def test_get_returns_last_blob_by_locator() -> None:
    sink = DownloadSink()
    blob = _blob(b"payload")
    r = await sink.put(blob)

    assert await sink.get(r.locator) == b"payload"


@pytest.mark.asyncio
async def test_get_unknown_locator_raises() -> None:
    sink = DownloadSink()
    with pytest.raises(SinkError):
        await sink.get("demo/platform/something.tar.gz")


@pytest.mark.asyncio
async def test_list_returns_empty() -> None:
    sink = DownloadSink()
    await sink.put(_blob(b"anything"))
    assert await sink.list_snapshots() == []


@pytest.mark.asyncio
async def test_delete_is_rejected() -> None:
    sink = DownloadSink()
    r = await sink.put(_blob(b"anything"))
    with pytest.raises(SinkError):
        await sink.delete(r.locator)
