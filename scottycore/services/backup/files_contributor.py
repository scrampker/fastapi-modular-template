"""FilesContributor — ships uploaded files alongside the DB backup.

For PLATFORM scope it walks the entire ``uploads_base_dir`` recursively; for
TENANT scope it walks only ``<uploads_base_dir>/<tenant_slug>/`` — the slug
is resolved from the tenant UUID via the injected session factory.

Files are stored in the tarball at ``files/files/<relative_path>`` (the outer
``files/`` is the tarball bucket, the inner ``files`` is this contributor_id).
Restore writes them back to the same relative location, creating parents as
needed. Existing files are overwritten; callers wanting a safer merge should
snapshot the uploads dir before restoring.

Per-file size cap
-----------------
``max_bytes_per_file`` (default 256 MiB) protects the in-memory tarball from
accidentally including a massive blob. Files over the cap are skipped with a
warning.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scottycore.services.backup.schemas import BackupScope, ContributorExport

_log = logging.getLogger(__name__)


class FilesContributor:
    """Back up tenant-uploaded files."""

    contributor_id = "files"
    scopes: set[BackupScope] = {BackupScope.PLATFORM, BackupScope.TENANT}
    description = "Tenant-uploaded files under uploads_base_dir."

    def __init__(
        self,
        uploads_base_dir: str | Path,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        *,
        max_bytes_per_file: int = 256 * 1024 * 1024,
    ) -> None:
        self._base = Path(uploads_base_dir).expanduser().resolve()
        self._session_factory = session_factory
        self._max_bytes = max_bytes_per_file

    async def export(
        self, scope: BackupScope, tenant_id: str | None
    ) -> ContributorExport:
        tenant_slug = await self._tenant_slug_for(scope, tenant_id)
        root = self._walk_root(scope, tenant_slug)
        files: list[tuple[str, bytes]] = []
        if root is None or not root.is_dir():
            return ContributorExport(files=files)

        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError as exc:
                _log.warning("stat failed for %s: %s", path, exc)
                continue
            if size > self._max_bytes:
                _log.warning(
                    "skipping %s (%d bytes > max %d)", path, size, self._max_bytes
                )
                continue
            try:
                raw = path.read_bytes()
            except OSError as exc:
                _log.warning("read failed for %s: %s", path, exc)
                continue
            rel = str(path.relative_to(self._base))
            files.append((rel, raw))
        return ContributorExport(files=files)

    async def restore(
        self,
        scope: BackupScope,
        tenant_id: str | None,
        rows: list[dict[str, Any]],
        files: list[tuple[str, bytes]],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> int:
        self._base.mkdir(parents=True, exist_ok=True)
        for rel, raw in files:
            target = self._base / rel
            try:
                target.resolve().relative_to(self._base)
            except ValueError:
                _log.warning("rejected file entry outside uploads root: %s", rel)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".part")
            tmp.write_bytes(raw)
            tmp.replace(target)
        return 0

    # ── internal ──────────────────────────────────────────────────────────

    def _walk_root(self, scope: BackupScope, tenant_slug: str | None) -> Path | None:
        if scope == BackupScope.PLATFORM:
            return self._base
        if not tenant_slug:
            return None
        return self._base / tenant_slug

    async def _tenant_slug_for(
        self, scope: BackupScope, tenant_id: str | None
    ) -> str | None:
        if scope != BackupScope.TENANT or tenant_id is None:
            return None
        if self._session_factory is None:
            return None
        from scottycore.services.tenants.models import Tenant

        async with self._session_factory() as s:
            t = (await s.scalars(select(Tenant).where(Tenant.id == tenant_id))).first()
        return t.slug if t else None
