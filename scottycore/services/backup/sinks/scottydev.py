"""ScottyDevSink — push/pull snapshots via ScottyDev's backup-store HTTP API.

This sink is symmetric: it's used by apps to ship backups *up* to ScottyDev,
and by ScottyDev itself (via the same class pointed at its own internal
API base) for dual-management (``managed_by = "scottydev"``) flows.

Protocol
--------
* ``PUT  /api/v1/backups/store/{app_slug}/{rel_path}`` — upload binary
* ``PUT  /api/v1/backups/store/{app_slug}/{rel_path}.meta.json`` — upload
  JSON sidecar (same schema as ``local_disk``)
* ``GET  /api/v1/backups/store/{app_slug}/{rel_path}`` — download binary
* ``GET  /api/v1/backups/index?app_slug=...`` — list snapshots (JSON array)
* ``DELETE /api/v1/backups/store/{app_slug}/{rel_path}`` — delete

Auth is Bearer token, provisioned per-app during workspace enrollment.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import ClassVar

import httpx

from scottycore.services.backup.sinks.base import (
    BackupBlob,
    SinkError,
    SinkNotFoundError,
    SinkWriteResult,
    SnapshotEntry,
    StorageSink,
    default_filename,
)

_DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=120.0, pool=10.0)


class ScottyDevSink(StorageSink):
    """Remote sink backed by ScottyDev's backup-store API."""

    sink_type: ClassVar[str] = "scottydev"

    def __init__(
        self,
        *,
        base_url: str,
        token: str | None = None,
        verify_tls: bool = True,
        client: httpx.AsyncClient | None = None,
    ):
        self._base = base_url.rstrip("/")
        self._token = token
        self._verify = verify_tls
        self._client = client

    async def put(self, blob: BackupBlob) -> SinkWriteResult:
        rel = default_filename(blob)
        url_bundle = f"{self._base}/api/v1/backups/store/{blob.app_slug}/{rel}"
        url_meta = url_bundle + ".meta.json"

        meta = {
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
        }

        async with self._session() as client:
            r1 = await client.put(
                url_bundle,
                content=blob.data,
                headers={"content-type": "application/gzip"},
            )
            _raise_for_status(r1, "bundle upload")
            r2 = await client.put(
                url_meta,
                content=json.dumps(meta).encode(),
                headers={"content-type": "application/json"},
            )
            _raise_for_status(r2, "sidecar upload")

        return SinkWriteResult(
            locator=f"{blob.app_slug}/{rel}",
            sink_type=self.sink_type,
            bytes_written=blob.size,
            created_at=blob.created_at,
        )

    async def get(self, locator: str) -> bytes:
        url = f"{self._base}/api/v1/backups/store/{locator}"
        async with self._session() as client:
            r = await client.get(url)
            if r.status_code == 404:
                raise SinkNotFoundError(f"no snapshot at {locator}")
            _raise_for_status(r, "bundle download")
            return r.content

    async def list_snapshots(
        self, *, app_slug: str | None = None, tenant_slug: str | None = None
    ) -> list[SnapshotEntry]:
        url = f"{self._base}/api/v1/backups/index"
        params: dict[str, str] = {}
        if app_slug:
            params["app_slug"] = app_slug
        if tenant_slug:
            params["tenant_slug"] = tenant_slug

        async with self._session() as client:
            r = await client.get(url, params=params)
            _raise_for_status(r, "index")
            rows = r.json()

        entries: list[SnapshotEntry] = []
        for row in rows:
            entries.append(
                SnapshotEntry(
                    locator=row["locator"],
                    app_slug=row.get("app_slug", ""),
                    scope=row.get("scope", ""),
                    kind=row.get("kind", "full"),
                    size=int(row.get("size", 0)),
                    created_at=_parse_ts(row.get("created_at")),
                    encrypted=bool(row.get("encrypted", False)),
                    sha256=row.get("sha256"),
                    key_fingerprint=row.get("key_fingerprint"),
                    tenant_slug=row.get("tenant_slug"),
                )
            )
        return entries

    async def delete(self, locator: str) -> None:
        url = f"{self._base}/api/v1/backups/store/{locator}"
        async with self._session() as client:
            r = await client.delete(url)
            if r.status_code == 404:
                raise SinkNotFoundError(f"no snapshot at {locator}")
            _raise_for_status(r, "delete")

    # ── internals ──────────────────────────────────────────────────────────

    def _session(self) -> "_Session":
        return _Session(self)


class _Session:
    """Context manager that yields the shared or a per-call httpx client."""

    def __init__(self, sink: ScottyDevSink):
        self._sink = sink
        self._own: httpx.AsyncClient | None = None

    async def __aenter__(self) -> httpx.AsyncClient:
        if self._sink._client is not None:
            return self._sink._client
        headers = {}
        if self._sink._token:
            headers["authorization"] = f"Bearer {self._sink._token}"
        self._own = httpx.AsyncClient(
            timeout=_DEFAULT_TIMEOUT,
            verify=self._sink._verify,
            headers=headers,
        )
        return self._own

    async def __aexit__(self, *exc_info: object) -> None:
        if self._own is not None:
            await self._own.aclose()


def _raise_for_status(r: httpx.Response, what: str) -> None:
    if r.status_code >= 400:
        raise SinkError(
            f"scottydev sink {what} failed: HTTP {r.status_code} {r.text[:400]}"
        )


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
