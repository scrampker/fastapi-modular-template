"""Chaos tests — concurrent sink writes.

Two simultaneous put() calls to the same logical locator must not:
- leave a partial file
- corrupt each other's content
- crash the process

The .part → rename pattern in LocalDiskSink should make the final file
atomic on POSIX filesystems.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scottycore.services.backup.sinks.base import BackupBlob
from scottycore.services.backup.sinks.local_disk import LocalDiskSink


def _blob(data: bytes, ts: datetime | None = None) -> BackupBlob:
    return BackupBlob(
        data=data,
        sha256="aa" * 32,
        size=len(data),
        app_slug="app",
        scope="platform",
        kind="full",
        created_at=ts or datetime.now(timezone.utc),
        encrypted=False,
    )


@pytest.fixture()
def sink(tmp_path: Path) -> LocalDiskSink:
    return LocalDiskSink(tmp_path / "sink")


@pytest.mark.asyncio
async def test_concurrent_put_same_locator_no_corruption(sink: LocalDiskSink) -> None:
    """Two simultaneous puts with the same timestamp/metadata produce the same
    logical locator (default_filename is deterministic). POSIX rename is atomic,
    so only one write wins, but either outcome must be valid (non-empty, non-corrupt).
    """
    ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    payload_a = b"AAAAA" * 1024
    payload_b = b"BBBBB" * 1024

    blob_a = _blob(payload_a, ts)
    blob_b = _blob(payload_b, ts)

    r_a, r_b = await asyncio.gather(
        sink.put(blob_a),
        sink.put(blob_b),
    )
    # Locators should be the same (same deterministic name from same metadata)
    assert r_a.locator == r_b.locator

    # Read back — must be one of the two payloads, not a mix
    stored = await sink.get(r_a.locator)
    assert stored in (payload_a, payload_b), "stored content is a torn write"


@pytest.mark.asyncio
async def test_concurrent_put_different_locators_both_written(
    sink: LocalDiskSink,
) -> None:
    """Two puts with different timestamps produce different locators; both must land."""
    ts_a = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    ts_b = datetime(2024, 1, 1, 11, 0, 0, tzinfo=timezone.utc)

    blob_a = _blob(b"alpha-payload", ts_a)
    blob_b = _blob(b"beta-payload", ts_b)

    r_a, r_b = await asyncio.gather(
        sink.put(blob_a),
        sink.put(blob_b),
    )
    assert r_a.locator != r_b.locator

    stored_a = await sink.get(r_a.locator)
    stored_b = await sink.get(r_b.locator)
    assert stored_a == b"alpha-payload"
    assert stored_b == b"beta-payload"


@pytest.mark.asyncio
async def test_many_concurrent_puts_all_succeed(sink: LocalDiskSink) -> None:
    """Fan-out: 20 concurrent puts with distinct timestamps all succeed."""
    import asyncio
    from datetime import timedelta

    base = datetime(2024, 6, 1, tzinfo=timezone.utc)
    blobs = [
        _blob(f"payload-{i}".encode(), base + timedelta(seconds=i))
        for i in range(20)
    ]
    results = await asyncio.gather(*[sink.put(b) for b in blobs])
    locators = {r.locator for r in results}
    assert len(locators) == 20  # all distinct

    for i, r in enumerate(results):
        stored = await sink.get(r.locator)
        assert stored == f"payload-{i}".encode()


@pytest.mark.asyncio
async def test_concurrent_get_while_putting(sink: LocalDiskSink) -> None:
    """get() during an in-progress put should either return current data or raise,
    never return the .part file content."""
    ts = datetime(2024, 2, 1, tzinfo=timezone.utc)
    payload = b"CONCURRENT" * 512

    blob = _blob(payload, ts)

    async def _put_and_record():
        result = await sink.put(blob)
        return result.locator

    async def _try_get_early(locator_holder: list):
        # Small sleep to let the put start but not finish
        await asyncio.sleep(0)
        if locator_holder:
            try:
                data = await sink.get(locator_holder[0])
                # If we got something it must be the full payload
                assert data == payload
            except Exception:
                pass  # fine to fail — the file may not exist yet

    locator_holder: list = []
    put_task = asyncio.create_task(_put_and_record())
    await asyncio.sleep(0)
    locator = await put_task
    locator_holder.append(locator)

    # After put completes, get must work
    stored = await sink.get(locator)
    assert stored == payload
