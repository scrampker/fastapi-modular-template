"""StorageSink ABC — where a backup bundle gets placed after export.

A sink owns the persistent storage mechanism (local disk, remote node over SSH,
ScottyDev central store, Forgejo-LFS, etc.). The BackupService hands a sink a
:class:`BackupBlob`; the sink is responsible for durability, listing, retrieval
and deletion.

``DownloadSink`` in :mod:`.download` is a special-case "sink" that returns the
bundle to the caller instead of persisting it — useful for ad-hoc browser
downloads. It implements only :meth:`put` and raises for the others.

Conventions
-----------
* ``locator`` is an opaque string unique within a sink that identifies a stored
  snapshot. Callers MUST treat it as opaque; the sink is free to change
  representation. It is stored in ``BackupRun.sink_locator``.
* Snapshots are named ``{app_slug}/{scope}/{timestamp}-{kind}.tar.gz.gpg``
  when encrypted, or ``...tar.gz`` otherwise (see :func:`default_filename`).
* Sinks never hold plaintext in memory longer than necessary. The BackupBlob
  payload is already encrypted when :meth:`put` is called (if requested).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import ClassVar


@dataclass(frozen=True)
class BackupBlob:
    """A fully-formed, ready-to-persist backup payload.

    ``data`` is the tarball bytes, already encrypted when ``encrypted`` is True.
    ``sha256`` is computed over ``data`` (post-encryption).
    """

    data: bytes
    sha256: str
    size: int
    app_slug: str
    scope: str  # "platform" | "tenant"
    kind: str  # "full" | "delta"
    created_at: datetime
    encrypted: bool = False
    key_fingerprint: str | None = None  # first 8 bytes of passphrase hash, hex
    tenant_slug: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SinkWriteResult:
    """Return value of :meth:`StorageSink.put`."""

    locator: str
    sink_type: str
    bytes_written: int
    created_at: datetime


@dataclass(frozen=True)
class SnapshotEntry:
    """A single snapshot listed by :meth:`StorageSink.list_snapshots`."""

    locator: str
    app_slug: str
    scope: str
    kind: str
    size: int
    created_at: datetime
    encrypted: bool
    sha256: str | None = None
    key_fingerprint: str | None = None
    tenant_slug: str | None = None


class SinkError(Exception):
    """Base error for sink operations."""


class SinkNotFoundError(SinkError):
    """Raised when a locator does not resolve to a snapshot."""


class StorageSink(ABC):
    """Persistent target for a backup bundle.

    Subclasses MUST be stateless w.r.t. a single call (any per-instance state
    is configuration, not per-op). Implementations should be coroutine-safe —
    multiple exports may overlap.
    """

    sink_type: ClassVar[str]

    @abstractmethod
    async def put(self, blob: BackupBlob) -> SinkWriteResult:
        """Persist *blob* and return a stable locator."""

    @abstractmethod
    async def get(self, locator: str) -> bytes:
        """Fetch the payload for *locator*. Raises :class:`SinkNotFoundError`."""

    @abstractmethod
    async def list_snapshots(
        self, *, app_slug: str | None = None, tenant_slug: str | None = None
    ) -> list[SnapshotEntry]:
        """Return known snapshots, optionally filtered."""

    @abstractmethod
    async def delete(self, locator: str) -> None:
        """Delete the snapshot at *locator*. Raises :class:`SinkNotFoundError`."""

    async def verify(self, locator: str, expected_sha256: str) -> bool:
        """Read back and re-hash. Default impl pulls the full blob.

        Override for sinks that store a checksum alongside the payload.
        """
        import hashlib

        data = await self.get(locator)
        actual = hashlib.sha256(data).hexdigest()
        return actual == expected_sha256


# ── Helpers ────────────────────────────────────────────────────────────────


def default_filename(blob: BackupBlob) -> str:
    """Deterministic filename for a blob — used by on-disk sinks."""
    ts = blob.created_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ext = ".tar.gz.gpg" if blob.encrypted else ".tar.gz"
    if blob.scope == "tenant" and blob.tenant_slug:
        name = f"{blob.app_slug}/{blob.scope}/{blob.tenant_slug}/{ts}-{blob.kind}{ext}"
    else:
        name = f"{blob.app_slug}/{blob.scope}/{ts}-{blob.kind}{ext}"
    return name
