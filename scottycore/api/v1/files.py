"""Files API routes — tenant-scoped file management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from scottycore.api.v1.helpers import build_audit_ctx
from scottycore.core.auth import require_role
from scottycore.core.dependencies import get_files_service, get_tenants_service
from scottycore.core.schemas import RoleName
from scottycore.services.auth.schemas import UserContext
from scottycore.services.files.schemas import (
    DeleteRequest,
    FileEntry,
    FileListResponse,
    MkdirRequest,
    RenameRequest,
)
from scottycore.services.files.service import FilesService
from scottycore.services.tenants.service import TenantsService

router = APIRouter()


@router.get("", response_model=FileListResponse)
async def list_files(
    slug: str,
    path: str = Query(default="", description="Relative path to list; empty for tenant root."),
    user: UserContext = Depends(require_role(RoleName.ANALYST)),
    svc: FilesService = Depends(get_files_service),
    tenants_svc: TenantsService = Depends(get_tenants_service),
) -> FileListResponse:
    """List files and folders at *path* inside the tenant's upload directory."""
    await tenants_svc.get_by_slug(slug)
    return await svc.list_files(slug, path)


@router.post("/mkdir", response_model=FileEntry, status_code=201)
async def mkdir(
    slug: str,
    body: MkdirRequest,
    request: Request,
    user: UserContext = Depends(require_role(RoleName.ANALYST)),
    svc: FilesService = Depends(get_files_service),
    tenants_svc: TenantsService = Depends(get_tenants_service),
) -> FileEntry:
    """Create a new directory inside the tenant's upload area. Requires analyst role or higher."""
    tenant = await tenants_svc.get_by_slug(slug)
    ctx = build_audit_ctx(user, request, tenant.id)
    return await svc.mkdir(slug, body.path, body.name, ctx)


@router.post("/rename", response_model=FileEntry)
async def rename(
    slug: str,
    body: RenameRequest,
    request: Request,
    user: UserContext = Depends(require_role(RoleName.ANALYST)),
    svc: FilesService = Depends(get_files_service),
    tenants_svc: TenantsService = Depends(get_tenants_service),
) -> FileEntry:
    """Rename a file or directory within the tenant's upload area. Requires analyst role or higher."""
    tenant = await tenants_svc.get_by_slug(slug)
    ctx = build_audit_ctx(user, request, tenant.id)
    return await svc.rename(slug, body.path, body.new_name, ctx)


@router.post("/delete", status_code=204)
async def delete(
    slug: str,
    body: DeleteRequest,
    request: Request,
    user: UserContext = Depends(require_role(RoleName.ANALYST)),
    svc: FilesService = Depends(get_files_service),
    tenants_svc: TenantsService = Depends(get_tenants_service),
) -> None:
    """Delete a file or empty directory. Requires analyst role or higher."""
    tenant = await tenants_svc.get_by_slug(slug)
    ctx = build_audit_ctx(user, request, tenant.id)
    await svc.delete(slug, body.path, ctx)
