"""LocalDiskSink — write/read snapshots to/from a filesystem directory.

Layout under ``root_dir``::

    {app_slug}/{scope}/{timestamp}-{kind}.tar.gz.gpg
    {app_slug}/{scope}/{timestamp}-{kind}.meta.json

``.meta.json`` sidecar carries the SHA-256, key fingerprint and size so
``list_snapshots`` does not need to read/decrypt the tarball to report them.
Missing sidecars are tolerated — the sink will stat the file and compute a
best-effort hash on demand.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

from scottycore.services.backup.sinks.base import (
    BackupBlob,
    SinkNotFoundError,
    SinkWriteResult,
    SnapshotEntry,
    StorageSink,
    default_filename,
)


class LocalDiskSink(StorageSink):
    """Filesystem-backed sink. Suitable for tests, dev, local-app stores."""

    sink_type: ClassVar[str] = "local_disk"

    def __init__(self, root_dir: str | Path):
        self._root = Path(root_dir).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    async def put(self, blob: BackupBlob) -> SinkWriteResult:
        rel = default_filename(blob)
        target = self._root / rel
        target.parent.mkdir(parents=True, exist_ok=True)

        def _write() -> None:
            tmp = target.with_suffix(target.suffix + ".part")
            tmp.write_bytes(blob.data)
            tmp.replace(target)
            sidecar = target.with_suffix(target.suffix + ".meta.json")
            sidecar.write_text(
                json.dumps(
                    {
                        "sha256": blob.sha256,
                        "size": blob.size,
                        "app_slug": blob.app_slug,
                        "scope": blob.scope,
                        "kind": blob.kind,
                        "encrypted": blob.encrypted,
                        "key_fingerprint": blob.key_fingerprint,
                        "tenant_slug": blob.tenant_slug,
                        "created_at": blob.created_at.isoformat(),
                        "metadata": blob.metadata,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

        await asyncio.to_thread(_write)
        return SinkWriteResult(
            locator=rel,
            sink_type=self.sink_type,
            bytes_written=blob.size,
            created_at=blob.created_at,
        )

    async def get(self, locator: str) -> bytes:
        path = self._resolve(locator)
        return await asyncio.to_thread(path.read_bytes)

    async def list_snapshots(
        self, *, app_slug: str | None = None, tenant_slug: str | None = None
    ) -> list[SnapshotEntry]:
        def _scan() -> list[SnapshotEntry]:
            out: list[SnapshotEntry] = []
            for sidecar in self._root.rglob("*.meta.json"):
                try:
                    meta = json.loads(sidecar.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if app_slug and meta.get("app_slug") != app_slug:
                    continue
                if tenant_slug and meta.get("tenant_slug") != tenant_slug:
                    continue
                bundle_path = sidecar.with_name(sidecar.name[: -len(".meta.json")])
                if not bundle_path.exists():
                    continue
                rel = str(bundle_path.relative_to(self._root))
                out.append(
                    SnapshotEntry(
                        locator=rel,
                        app_slug=meta.get("app_slug", ""),
                        scope=meta.get("scope", ""),
                        kind=meta.get("kind", "full"),
                        size=int(meta.get("size", bundle_path.stat().st_size)),
                        created_at=_parse_ts(meta.get("created_at")),
                        encrypted=bool(meta.get("encrypted", False)),
                        sha256=meta.get("sha256"),
                        key_fingerprint=meta.get("key_fingerprint"),
                        tenant_slug=meta.get("tenant_slug"),
                    )
                )
            out.sort(key=lambda e: e.created_at, reverse=True)
            return out

        return await asyncio.to_thread(_scan)

    async def delete(self, locator: str) -> None:
        path = self._resolve(locator)

        def _unlink() -> None:
            path.unlink()
            sidecar = path.with_suffix(path.suffix + ".meta.json")
            if sidecar.exists():
                sidecar.unlink()

        await asyncio.to_thread(_unlink)

    async def verify(self, locator: str, expected_sha256: str) -> bool:
        """Stream-hash without loading whole file into memory."""
        path = self._resolve(locator)

        def _hash() -> str:
            h = hashlib.sha256()
            with path.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest()

        return (await asyncio.to_thread(_hash)) == expected_sha256

    # ── internals ──────────────────────────────────────────────────────────

    def _resolve(self, locator: str) -> Path:
        target = (self._root / locator).resolve()
        if not _is_within(self._root, target):
            raise SinkNotFoundError(f"locator escapes root: {locator}")
        if not target.is_file():
            raise SinkNotFoundError(f"no snapshot at {locator}")
        return target


def _is_within(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def _parse_ts(raw: object) -> datetime:
    if not isinstance(raw, str):
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
