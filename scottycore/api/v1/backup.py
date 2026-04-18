"""Backup REST API — export/restore/list/runs/schedules.

All endpoints require superadmin at the platform scope. Tenant-scope exports
check tenant-admin of the specified tenant; validation lives inside the
service layer (audit entries identify the caller either way).

The *store* endpoints under ``/api/v1/backups/store/...`` implement the
protocol consumed by :class:`scottycore.services.backup.sinks.ScottyDevSink`
so ScottyDev can act as a remote sink target for other apps.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scottycore.core.auth import UserContext, require_auth, require_superadmin
from scottycore.core.dependencies import _registry
from scottycore.core.service_registry import ServiceRegistry
from scottycore.services.backup.crypto import decrypt_bundle, encrypt_bundle, fingerprint
from scottycore.services.backup.models import (
    BackupRun,
    BackupSchedule,
)
from scottycore.services.backup.schemas import (
    BackupScope,
    ContributorInfo,
    RestoreSummary,
)
from scottycore.services.backup.service import BackupService, UnsupportedBundleError
from scottycore.services.backup.sinks import (
    BackupBlob,
    DownloadSink,
    LocalDiskSink,
    ScottyDevSink,
    SinkError,
    SinkNotFoundError,
    StorageSink,
)

router = APIRouter()


# ── Request/response schemas ──────────────────────────────────────────────


class ExportRequest(BaseModel):
    scope: BackupScope = BackupScope.PLATFORM
    tenant_slug: str | None = None
    sink_type: str = "download"
    sink_config: dict = Field(default_factory=dict)
    passphrase: str | None = None


class ExportResponse(BaseModel):
    locator: str
    sink_type: str
    size: int
    sha256: str
    encrypted: bool
    key_fingerprint: str | None


class RestoreRequest(BaseModel):
    sink_type: str
    locator: str
    sink_config: dict = Field(default_factory=dict)
    passphrase: str | None = None


class ScheduleCreate(BaseModel):
    name: str
    scope: BackupScope = BackupScope.PLATFORM
    tenant_slug: str | None = None
    sink_type: str
    sink_config: dict = Field(default_factory=dict)
    cron_expr: str | None = None
    kind: str = "full"
    retention_days: int | None = None
    keep_last: int | None = None


class ScheduleOut(BaseModel):
    id: str
    name: str
    scope: str
    tenant_slug: str | None
    sink_type: str
    cron_expr: str | None
    kind: str
    managed_by: str
    is_active: bool
    next_run_at: datetime | None
    created_at: datetime


class RunOut(BaseModel):
    id: str
    schedule_id: str | None
    app_slug: str
    scope: str
    kind: str
    status: str
    sink_type: str
    sink_locator: str | None
    bytes_written: int | None
    sha256: str | None
    encrypted: bool
    key_fingerprint: str | None
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    created_at: datetime


# ── Helpers ────────────────────────────────────────────────────────────────


def get_backup_service(reg: ServiceRegistry = Depends(_registry)) -> BackupService:
    return reg.backup


def _session_factory(
    reg: ServiceRegistry = Depends(_registry),
) -> async_sessionmaker[AsyncSession]:
    # ServiceRegistry doesn't expose it directly; pull from .audit (which holds
    # the session factory as _session_factory). Keep the import local to avoid
    # a public dependency on the private attribute name everywhere.
    return reg.audit._session_factory  # type: ignore[attr-defined]


def _build_sink(sink_type: str, sink_config: dict) -> StorageSink:
    """Construct a sink from a (type, config) pair coming from API input."""
    if sink_type == "download":
        return DownloadSink()
    if sink_type == "local_disk":
        root = sink_config.get("root_dir") or "/app/data/backups"
        return LocalDiskSink(root)
    if sink_type == "scottydev":
        base = sink_config.get("base_url")
        if not base:
            raise HTTPException(400, "orchestrator sink requires base_url")
        return ScottyDevSink(base_url=base, token=sink_config.get("token"))
    if sink_type == "git_repo":
        from scottycore.services.backup.sinks import GitRepoSink

        repo_url = sink_config.get("repo_url")
        if not repo_url:
            raise HTTPException(400, "git_repo sink requires repo_url")
        clone_dir = sink_config.get("clone_dir") or "/app/data/backups-git"
        return GitRepoSink(
            repo_url=repo_url,
            local_clone_dir=clone_dir,
            branch=sink_config.get("branch") or "backups",
            path_template=(
                sink_config.get("path_template")
                or "snapshots/{app_slug}/{scope}/{tenant_slug}"
            ),
            lfs_enabled=bool(sink_config.get("lfs_enabled", True)),
        )
    raise HTTPException(400, f"unsupported sink_type: {sink_type}")


def _blob_from_bytes(
    data: bytes,
    *,
    app_slug: str,
    scope: BackupScope,
    tenant_slug: str | None,
    encrypted: bool,
    key_fp: str | None,
) -> BackupBlob:
    return BackupBlob(
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        app_slug=app_slug,
        scope=scope.value,
        kind="full",
        created_at=datetime.now(timezone.utc),
        encrypted=encrypted,
        key_fingerprint=key_fp,
        tenant_slug=tenant_slug,
    )


# ── Contributors (read-only) ──────────────────────────────────────────────


@router.get("/contributors", response_model=list[ContributorInfo])
async def list_contributors(
    scope: BackupScope | None = None,
    svc: BackupService = Depends(get_backup_service),
    _: UserContext = Depends(require_superadmin),
) -> list[ContributorInfo]:
    return svc.list_contributors(scope)


# ── Export ────────────────────────────────────────────────────────────────


@router.post("/export", response_model=ExportResponse)
async def export(
    request: Request,
    payload: ExportRequest,
    svc: BackupService = Depends(get_backup_service),
    user: UserContext = Depends(require_superadmin),
    factory: async_sessionmaker[AsyncSession] = Depends(_session_factory),
) -> ExportResponse:
    ip = request.client.host if request.client else ""
    if payload.scope == BackupScope.PLATFORM:
        bundle = await svc.export_platform(user_id=user.user_id, ip=ip)
        tenant_slug = None
    else:
        if not payload.tenant_slug:
            raise HTTPException(400, "tenant_slug is required for TENANT scope")
        # Resolve tenant_id from slug.
        from scottycore.services.tenants.models import Tenant

        async with factory() as s:
            t = (
                await s.scalars(select(Tenant).where(Tenant.slug == payload.tenant_slug))
            ).first()
        if t is None:
            raise HTTPException(404, "tenant not found")
        bundle = await svc.export_tenant(
            tenant_id=str(t.id),
            tenant_slug=t.slug,
            user_id=user.user_id,
            ip=ip,
        )
        tenant_slug = t.slug

    key_fp: str | None = None
    if payload.passphrase:
        bundle = await encrypt_bundle(bundle, payload.passphrase)
        key_fp = fingerprint(payload.passphrase)

    blob = _blob_from_bytes(
        bundle,
        app_slug=svc._app_name,  # noqa: SLF001
        scope=payload.scope,
        tenant_slug=tenant_slug,
        encrypted=bool(payload.passphrase),
        key_fp=key_fp,
    )

    sink = _build_sink(payload.sink_type, payload.sink_config)
    result = await sink.put(blob)

    # Record a BackupRun row.
    async with factory() as s:
        run = BackupRun(
            app_slug=svc._app_name,  # noqa: SLF001
            scope=payload.scope.value,
            tenant_slug=tenant_slug,
            kind="full",
            status="success",
            sink_type=sink.sink_type,
            sink_locator=result.locator,
            bytes_written=result.bytes_written,
            sha256=blob.sha256,
            encrypted=blob.encrypted,
            key_fingerprint=key_fp,
            started_at=blob.created_at,
            finished_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    return ExportResponse(
        locator=result.locator,
        sink_type=sink.sink_type,
        size=blob.size,
        sha256=blob.sha256,
        encrypted=blob.encrypted,
        key_fingerprint=key_fp,
    )


@router.post("/export/download")
async def export_download(
    request: Request,
    payload: ExportRequest,
    svc: BackupService = Depends(get_backup_service),
    user: UserContext = Depends(require_superadmin),
    factory: async_sessionmaker[AsyncSession] = Depends(_session_factory),
) -> StreamingResponse:
    """Stream a full platform/tenant export straight back to the browser."""
    payload = payload.model_copy(update={"sink_type": "download"})
    ip = request.client.host if request.client else ""

    if payload.scope == BackupScope.PLATFORM:
        bundle = await svc.export_platform(user_id=user.user_id, ip=ip)
        tenant_slug = None
    else:
        if not payload.tenant_slug:
            raise HTTPException(400, "tenant_slug is required for TENANT scope")
        from scottycore.services.tenants.models import Tenant

        async with factory() as s:
            t = (
                await s.scalars(select(Tenant).where(Tenant.slug == payload.tenant_slug))
            ).first()
        if t is None:
            raise HTTPException(404, "tenant not found")
        bundle = await svc.export_tenant(
            tenant_id=str(t.id),
            tenant_slug=t.slug,
            user_id=user.user_id,
            ip=ip,
        )
        tenant_slug = t.slug

    if payload.passphrase:
        bundle = await encrypt_bundle(bundle, payload.passphrase)
        fname_ext = "tar.gz.gpg"
    else:
        fname_ext = "tar.gz"

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    scope_part = payload.scope.value if tenant_slug is None else f"tenant-{tenant_slug}"
    filename = f"{svc._app_name}-{scope_part}-{ts}.{fname_ext}"  # noqa: SLF001

    return Response(
        content=bundle,
        media_type="application/gzip",
        headers={"content-disposition": f'attachment; filename="{filename}"'},
    )


# ── Restore ───────────────────────────────────────────────────────────────


@router.post("/restore", response_model=RestoreSummary)
async def restore(
    request: Request,
    payload: RestoreRequest,
    svc: BackupService = Depends(get_backup_service),
    user: UserContext = Depends(require_superadmin),
) -> RestoreSummary:
    sink = _build_sink(payload.sink_type, payload.sink_config)
    try:
        data = await sink.get(payload.locator)
    except SinkNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except SinkError as exc:
        raise HTTPException(502, str(exc)) from exc

    if payload.passphrase:
        from scottycore.services.backup.crypto import CryptoError

        try:
            data = await decrypt_bundle(data, payload.passphrase)
        except CryptoError as exc:
            raise HTTPException(400, f"decrypt failed: {exc}") from exc

    ip = request.client.host if request.client else ""
    try:
        return await svc.restore_bundle(data, user_id=user.user_id, ip=ip)
    except UnsupportedBundleError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/restore/upload", response_model=RestoreSummary)
async def restore_upload(
    request: Request,
    bundle: UploadFile,
    passphrase: str | None = None,
    svc: BackupService = Depends(get_backup_service),
    user: UserContext = Depends(require_superadmin),
) -> RestoreSummary:
    """Alternative restore path: upload the tarball directly from the browser."""
    data = await bundle.read()
    if passphrase:
        from scottycore.services.backup.crypto import CryptoError

        try:
            data = await decrypt_bundle(data, passphrase)
        except CryptoError as exc:
            raise HTTPException(400, f"decrypt failed: {exc}") from exc
    ip = request.client.host if request.client else ""
    try:
        return await svc.restore_bundle(data, user_id=user.user_id, ip=ip)
    except UnsupportedBundleError as exc:
        raise HTTPException(409, str(exc)) from exc


# ── Runs & schedules ──────────────────────────────────────────────────────


@router.get("/runs", response_model=list[RunOut])
async def list_runs(
    limit: int = 50,
    schedule_id: str | None = None,
    factory: async_sessionmaker[AsyncSession] = Depends(_session_factory),
    _: UserContext = Depends(require_superadmin),
) -> list[RunOut]:
    async with factory() as s:
        q = select(BackupRun).order_by(BackupRun.created_at.desc()).limit(limit)
        if schedule_id:
            q = q.where(BackupRun.schedule_id == schedule_id)
        rows = list((await s.scalars(q)).all())
    return [RunOut.model_validate(r, from_attributes=True) for r in rows]


@router.get("/schedules", response_model=list[ScheduleOut])
async def list_schedules(
    managed_by: str | None = None,
    factory: async_sessionmaker[AsyncSession] = Depends(_session_factory),
    _: UserContext = Depends(require_superadmin),
) -> list[ScheduleOut]:
    async with factory() as s:
        q = select(BackupSchedule).order_by(BackupSchedule.created_at.desc())
        if managed_by:
            q = q.where(BackupSchedule.managed_by == managed_by)
        rows = list((await s.scalars(q)).all())
    return [ScheduleOut.model_validate(r, from_attributes=True) for r in rows]


@router.post("/schedules", response_model=ScheduleOut)
async def create_schedule(
    payload: ScheduleCreate,
    factory: async_sessionmaker[AsyncSession] = Depends(_session_factory),
    user: UserContext = Depends(require_superadmin),
) -> ScheduleOut:
    async with factory() as s:
        row = BackupSchedule(
            name=payload.name,
            scope=payload.scope.value,
            tenant_slug=payload.tenant_slug,
            sink_type=payload.sink_type,
            sink_config=payload.sink_config,
            cron_expr=payload.cron_expr,
            kind=payload.kind,
            retention_days=payload.retention_days,
            keep_last=payload.keep_last,
            created_by=str(user.user_id),
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
    return ScheduleOut.model_validate(row, from_attributes=True)


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(
    schedule_id: str,
    factory: async_sessionmaker[AsyncSession] = Depends(_session_factory),
    _: UserContext = Depends(require_superadmin),
) -> dict:
    async with factory() as s:
        row = await s.get(BackupSchedule, schedule_id)
        if row is None:
            raise HTTPException(404, "schedule not found")
        if row.managed_by != "local":
            from scottycore.core.brand import get_brand

            orchestrator = (
                f"{get_brand().display_name}Dev"
                if get_brand().orchestrator_name
                == f"{get_brand().family_name}dev"
                else get_brand().orchestrator_name
            )
            raise HTTPException(
                409,
                f"schedule is managed by {orchestrator} — "
                "detach or promote before delete",
            )
        await s.delete(row)
        await s.commit()
    return {"deleted": schedule_id}


# ── Sink store (served by ScottyDev so apps can use ScottyDevSink) ────────
#
# These endpoints are what ``ScottyDevSink`` hits. They're included in the
# scottycore router so any app (including scottydev itself) that wants to
# *receive* backups exposes them automatically. Access is superadmin-scoped
# for now; enrollment-scoped tokens will replace superadmin in a later pass.


@router.put("/store/{rest:path}")
async def sink_store_put(
    request: Request,
    rest: str,
    factory: async_sessionmaker[AsyncSession] = Depends(_session_factory),
    _: UserContext = Depends(require_superadmin),
) -> dict:
    root = _sink_store_root()
    target = (root / rest).resolve()
    if not str(target).startswith(str(root)):
        raise HTTPException(400, "path escapes store root")
    target.parent.mkdir(parents=True, exist_ok=True)
    body = await request.body()
    target.write_bytes(body)
    return {"stored": rest, "bytes": len(body)}


@router.get("/store/{rest:path}")
async def sink_store_get(
    rest: str,
    _: UserContext = Depends(require_superadmin),
) -> Response:
    root = _sink_store_root()
    target = (root / rest).resolve()
    if not str(target).startswith(str(root)):
        raise HTTPException(400, "path escapes store root")
    if not target.is_file():
        raise HTTPException(404, "not found")
    return Response(content=target.read_bytes(), media_type="application/octet-stream")


@router.delete("/store/{rest:path}")
async def sink_store_delete(
    rest: str,
    _: UserContext = Depends(require_superadmin),
) -> dict:
    root = _sink_store_root()
    target = (root / rest).resolve()
    if not str(target).startswith(str(root)):
        raise HTTPException(400, "path escapes store root")
    if not target.is_file():
        raise HTTPException(404, "not found")
    target.unlink()
    sidecar = target.with_suffix(target.suffix + ".meta.json")
    if sidecar.is_file():
        sidecar.unlink()
    return {"deleted": rest}


@router.get("/index")
async def sink_store_index(
    app_slug: str | None = None,
    tenant_slug: str | None = None,
    _: UserContext = Depends(require_superadmin),
) -> list[dict]:
    root = _sink_store_root()
    out: list[dict] = []
    if not root.exists():
        return out
    for sidecar in root.rglob("*.meta.json"):
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if app_slug and meta.get("app_slug") != app_slug:
            continue
        if tenant_slug and meta.get("tenant_slug") != tenant_slug:
            continue
        bundle = sidecar.with_name(sidecar.name[: -len(".meta.json")])
        if not bundle.exists():
            continue
        rel = str(bundle.relative_to(root))
        out.append({"locator": rel, **meta})
    out.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return out


def _sink_store_root() -> Path:
    """Root dir for backup blobs received from other apps via ``/store/*``."""
    import os

    root = Path(os.environ.get("SCOTTYCORE_BACKUP_STORE", "/app/data/backup-store"))
    root.mkdir(parents=True, exist_ok=True)
    return root
