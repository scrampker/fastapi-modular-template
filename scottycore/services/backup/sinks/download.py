"""DownloadSink — returns the bundle as bytes for ad-hoc browser download.

Not a persistent sink: it captures the blob in memory and surfaces it to the
caller so the HTTP layer can stream it as a file response. ``get``/``list``/
``delete`` all raise — the caller already holds the bytes.
"""

from __future__ import annotations

from typing import ClassVar

from scottycore.services.backup.sinks.base import (
    BackupBlob,
    SinkError,
    SinkWriteResult,
    SnapshotEntry,
    StorageSink,
    default_filename,
)


class DownloadSink(StorageSink):
    """Ephemeral sink — bundle flows through to the HTTP response."""

    sink_type: ClassVar[str] = "download"

    def __init__(self) -> None:
        self._last: BackupBlob | None = None
        self._last_locator: str | None = None

    @property
    def last(self) -> BackupBlob | None:
        """Most recent blob handed to :meth:`put` — the caller reads this
        and streams it to the user."""
        return self._last

    async def put(self, blob: BackupBlob) -> SinkWriteResult:
        self._last = blob
        self._last_locator = default_filename(blob)
        return SinkWriteResult(
            locator=self._last_locator,
            sink_type=self.sink_type,
            bytes_written=blob.size,
            created_at=blob.created_at,
        )

    async def get(self, locator: str) -> bytes:
        if self._last is None or locator != self._last_locator:
            raise SinkError("download sink does not persist snapshots")
        return self._last.data

    async def list_snapshots(
        self, *, app_slug: str | None = None, tenant_slug: str | None = None
    ) -> list[SnapshotEntry]:
        return []

    async def delete(self, locator: str) -> None:
        raise SinkError("download sink does not persist snapshots")
