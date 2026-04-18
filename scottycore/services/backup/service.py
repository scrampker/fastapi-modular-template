"""BackupService — orchestrates export and restore across all contributors.

Design notes
------------
* All tarball work is in-memory (io.BytesIO + tarfile).  Bundles are not
  written to disk.  This keeps the service portable and easy to test.
* Contributor registration order is preserved (insertion-ordered dict).
  Restore iterates contributors in registration order, so callers must
  register low-level contributors (users, tenants) before high-level ones
  (roles, items) to satisfy FK constraints.
* Audit is fire-and-forget — a failed audit write never aborts a backup.
* ``export_*`` and ``restore_bundle`` are callable from tests without HTTP.
"""

from __future__ import annotations

import io
import json
import tarfile
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scottycore.services.audit.schemas import AuditLogCreate
from scottycore.services.audit.service import AuditService
from scottycore.services.backup.contributor import BackupContributor
from scottycore.services.backup.schemas import (
    SUPPORTED_SCHEMA_VERSION,
    BackupManifest,
    BackupScope,
    ContributorInfo,
    ManifestContributorEntry,
    RestoreSummary,
)


class UnsupportedBundleError(ValueError):
    """Raised when a bundle's schema_version exceeds what this build supports."""

class BackupService:
    """Coordinates backup and restore across all registered contributors."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        audit_service: AuditService,
        app_name: str | None = None,
        app_version: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._audit = audit_service
        # app_name/app_version get embedded in the backup tarball manifest so the
        # archive is self-describing. Default to settings values; consumer apps
        # can override explicitly for multi-app deployments.
        if app_name is None or app_version is None:
            from scottycore.core.config import get_settings
            s = get_settings()
            app_name = app_name or s.app_name
            app_version = app_version or getattr(s, "app_version", "0.0.0")
        self._app_name = app_name
        self._app_version = app_version
        # Insertion-ordered so restore runs in a deterministic, FK-safe sequence.
        self._contributors: dict[str, BackupContributor] = {}

    # ── Registration ──────────────────────────────────────────────────────

    def register(self, contributor: BackupContributor) -> None:
        """Register a contributor.  Later registrations silently overwrite earlier ones."""
        self._contributors[contributor.contributor_id] = contributor

    def list_contributors(self, scope: BackupScope | None = None) -> list[ContributorInfo]:
        """Return metadata for all contributors, optionally filtered by scope."""
        result: list[ContributorInfo] = []
        for c in self._contributors.values():
            if scope is not None and scope not in c.scopes:
                continue
            result.append(
                ContributorInfo(
                    id=c.contributor_id,
                    scopes=sorted(c.scopes, key=lambda s: s.value),
                    description=c.description,
                )
            )
        return result

    # ── Export ────────────────────────────────────────────────────────────

    async def export_platform(self, user_id: UUID, ip: str) -> bytes:
        """Export all PLATFORM-scope contributors to a .tar.gz bundle.

        Returns raw bytes.  Caller is responsible for streaming to client.
        Audit action: ``backup.platform.export``.
        """
        bundle = await self._run_export(BackupScope.PLATFORM, tenant_id=None, tenant_slug=None)
        await self._audit.log(AuditLogCreate(
            user_id=user_id,
            action="backup.platform.export",
            target_type="system",
            target_id="",
            ip_address=ip,
        ))
        return bundle

    async def export_platform_delta(
        self,
        since: datetime | None,
        exclude_origins: list[str] | None,
        user_id: UUID,
        ip: str,
    ) -> bytes:
        """Export a delta of PLATFORM-scope contributors since *since*.

        Contributors that implement ``export_delta()`` are called with the
        ``since`` and ``exclude_origins`` arguments; others fall back to a
        full ``export()`` call.  The engine applies an additional in-Python
        origin filter after receiving the bundle.
        """
        bundle = await self._run_delta_export(
            BackupScope.PLATFORM,
            tenant_id=None,
            tenant_slug=None,
            since=since,
            exclude_origins=exclude_origins or [],
        )
        await self._audit.log(AuditLogCreate(
            user_id=user_id,
            action="backup.platform.export_delta",
            target_type="system",
            target_id="",
            ip_address=ip,
        ))
        return bundle

    async def export_tenant_delta(
        self,
        tenant_id: str,
        tenant_slug: str,
        since: datetime | None,
        exclude_origins: list[str] | None,
        user_id: UUID,
        ip: str,
    ) -> bytes:
        """Export a delta of TENANT-scope contributors since *since*."""
        bundle = await self._run_delta_export(
            BackupScope.TENANT,
            tenant_id=tenant_id,
            tenant_slug=tenant_slug,
            since=since,
            exclude_origins=exclude_origins or [],
        )
        await self._audit.log(AuditLogCreate(
            user_id=user_id,
            tenant_id=UUID(tenant_id),
            action="backup.tenant.export_delta",
            target_type="tenant",
            target_id=tenant_id,
            ip_address=ip,
        ))
        return bundle

    async def export_tenant(
        self,
        tenant_id: str,
        tenant_slug: str,
        user_id: UUID,
        ip: str,
    ) -> bytes:
        """Export all TENANT-scope contributors for *tenant_id* to a .tar.gz bundle.

        Audit action: ``backup.tenant.export``.
        """
        bundle = await self._run_export(BackupScope.TENANT, tenant_id=tenant_id, tenant_slug=tenant_slug)
        await self._audit.log(AuditLogCreate(
            user_id=user_id,
            tenant_id=UUID(tenant_id),
            action="backup.tenant.export",
            target_type="tenant",
            target_id=tenant_id,
            ip_address=ip,
        ))
        return bundle

    # ── Restore ───────────────────────────────────────────────────────────

    async def restore_bundle(
        self,
        bundle_bytes: bytes,
        user_id: UUID,
        ip: str,
    ) -> RestoreSummary:
        """Restore a backup bundle.

        Reads the manifest to determine scope and tenant, then calls each
        contributor's restore() in registration order.

        Audit action: ``backup.platform.restore`` or ``backup.tenant.restore``.
        """
        manifest, contributor_data = self._extract_bundle(bundle_bytes)

        if manifest.schema_version > SUPPORTED_SCHEMA_VERSION:
            from scottycore.core.brand import get_brand

            brand = get_brand()
            raise UnsupportedBundleError(
                f"bundle schema_version={manifest.schema_version} exceeds "
                f"supported={SUPPORTED_SCHEMA_VERSION}; upgrade "
                f"{brand.framework_name} to restore"
            )

        scope = manifest.scope
        tenant_id: str | None = None
        if scope == BackupScope.TENANT and manifest.tenant_slug:
            # Resolve tenant_id from the manifest data itself (tenants contributor
            # must be present for PLATFORM; for TENANT scope the id is in the rows).
            tenant_id = self._resolve_tenant_id_from_bundle(manifest, contributor_data)

        summary = RestoreSummary(scope=scope, tenant_slug=manifest.tenant_slug)
        total = 0

        for contributor_id, c in self._contributors.items():
            if scope not in c.scopes:
                continue
            if contributor_id not in contributor_data:
                continue

            rows = contributor_data[contributor_id].get("rows", [])
            files = contributor_data[contributor_id].get("files", [])

            try:
                upserted = await c.restore(
                    scope=scope,
                    tenant_id=tenant_id,
                    rows=rows,
                    files=files,
                    session_factory=self._session_factory,
                )
                total += upserted
                summary.contributors_restored.append(contributor_id)
            except Exception as exc:
                summary.warnings.append(f"{contributor_id}: {exc}")

        summary.total_rows_upserted = total

        audit_action = (
            "backup.platform.restore" if scope == BackupScope.PLATFORM
            else "backup.tenant.restore"
        )
        audit_tenant_id = UUID(tenant_id) if tenant_id else None
        await self._audit.log(AuditLogCreate(
            user_id=user_id,
            tenant_id=audit_tenant_id,
            action=audit_action,
            target_type="system" if scope == BackupScope.PLATFORM else "tenant",
            target_id="" if scope == BackupScope.PLATFORM else (tenant_id or ""),
            detail={
                "contributors": summary.contributors_restored,
                "total_rows": total,
                "warnings": summary.warnings,
            },
            ip_address=ip,
        ))
        return summary

    # ── Internal helpers ──────────────────────────────────────────────────

    async def _run_delta_export(
        self,
        scope: BackupScope,
        tenant_id: str | None,
        tenant_slug: str | None,
        since: datetime | None,
        exclude_origins: list[str],
    ) -> bytes:
        """Call every matching contributor for a delta export.

        If the contributor implements ``export_delta()``, that is called;
        otherwise falls back to the full ``export()`` with no filtering.
        """
        manifest_entries: list[ManifestContributorEntry] = []
        contributor_payloads: dict[str, Any] = {}

        for cid, c in self._contributors.items():
            if scope not in c.scopes:
                continue
            if hasattr(c, "export_delta"):
                export = await c.export_delta(
                    scope=scope,
                    tenant_id=tenant_id,
                    since=since,
                    exclude_origins=exclude_origins,
                )
            else:
                export = await c.export(scope=scope, tenant_id=tenant_id)
            contributor_payloads[cid] = export
            manifest_entries.append(
                ManifestContributorEntry(
                    id=cid,
                    rows=len(export.rows),
                    files=len(export.files),
                )
            )

        manifest = BackupManifest(
            scope=scope,
            tenant_slug=tenant_slug,
            timestamp=datetime.now(timezone.utc),
            app_name=self._app_name,
            app_version=self._app_version,
            contributors=manifest_entries,
        )
        return self._build_tarball(manifest, contributor_payloads)

    async def _run_export(
        self,
        scope: BackupScope,
        tenant_id: str | None,
        tenant_slug: str | None,
    ) -> bytes:
        """Call every matching contributor and pack results into a tarball."""
        manifest_entries: list[ManifestContributorEntry] = []
        contributor_payloads: dict[str, Any] = {}

        for cid, c in self._contributors.items():
            if scope not in c.scopes:
                continue
            export = await c.export(scope=scope, tenant_id=tenant_id)
            contributor_payloads[cid] = export
            manifest_entries.append(
                ManifestContributorEntry(
                    id=cid,
                    rows=len(export.rows),
                    files=len(export.files),
                )
            )

        manifest = BackupManifest(
            scope=scope,
            tenant_slug=tenant_slug,
            timestamp=datetime.now(timezone.utc),
            app_name=self._app_name,
            app_version=self._app_version,
            contributors=manifest_entries,
        )
        return self._build_tarball(manifest, contributor_payloads)

    def _build_tarball(
        self,
        manifest: BackupManifest,
        contributor_payloads: dict[str, Any],
    ) -> bytes:
        """Pack manifest + contributor data into an in-memory .tar.gz."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            # manifest.json
            manifest_bytes = manifest.model_dump_json(indent=2).encode()
            _add_bytes(tf, "manifest.json", manifest_bytes)

            for cid, export in contributor_payloads.items():
                # data/<contributor_id>.json
                rows_bytes = json.dumps(export.rows, default=_json_default).encode()
                _add_bytes(tf, f"data/{cid}.json", rows_bytes)

                # files/<contributor_id>/<relative_path>
                for rel_path, raw in export.files:
                    _add_bytes(tf, f"files/{cid}/{rel_path}", raw)

        return buf.getvalue()

    def _extract_bundle(
        self, bundle_bytes: bytes
    ) -> tuple[BackupManifest, dict[str, dict[str, Any]]]:
        """Extract manifest and per-contributor rows/files from a tarball."""
        buf = io.BytesIO(bundle_bytes)
        contributor_data: dict[str, dict[str, Any]] = {}

        with tarfile.open(fileobj=buf, mode="r:gz") as tf:
            manifest_member = tf.getmember("manifest.json")
            manifest_fobj = tf.extractfile(manifest_member)
            if manifest_fobj is None:
                raise ValueError("Invalid backup: missing manifest.json")
            manifest = BackupManifest.model_validate_json(manifest_fobj.read())

            for member in tf.getmembers():
                name = member.name
                if name.startswith("data/") and name.endswith(".json"):
                    cid = name[len("data/"):-len(".json")]
                    fobj = tf.extractfile(member)
                    if fobj is not None:
                        contributor_data.setdefault(cid, {"rows": [], "files": []})
                        contributor_data[cid]["rows"] = json.loads(fobj.read())

                elif name.startswith("files/"):
                    # files/<contributor_id>/<rel_path>
                    parts = name.split("/", 2)
                    if len(parts) == 3:
                        _, cid, rel_path = parts
                        fobj = tf.extractfile(member)
                        if fobj is not None:
                            contributor_data.setdefault(cid, {"rows": [], "files": []})
                            contributor_data[cid]["files"].append((rel_path, fobj.read()))

        return manifest, contributor_data

    def _resolve_tenant_id_from_bundle(
        self,
        manifest: BackupManifest,
        contributor_data: dict[str, dict[str, Any]],
    ) -> str | None:
        """Try to recover the tenant UUID from the tenants contributor rows.

        Falls back to None; individual contributors that need the tenant_id
        can look it up themselves if necessary.
        """
        tenants_rows = contributor_data.get("tenants", {}).get("rows", [])
        for row in tenants_rows:
            if row.get("slug") == manifest.tenant_slug:
                return str(row.get("id"))
        return None


# ── Tarball helpers ────────────────────────────────────────────────────────


def _add_bytes(tf: tarfile.TarFile, arcname: str, data: bytes) -> None:
    """Add *data* as a file entry *arcname* inside the open tarfile."""
    info = tarfile.TarInfo(name=arcname)
    info.size = len(data)
    tf.addfile(info, io.BytesIO(data))


def _json_default(obj: Any) -> Any:
    """Fallback JSON encoder for types not handled by the stdlib encoder."""
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, bytes):
        import base64
        return base64.b64encode(obj).decode()
    return str(obj)
