"""Files service — tenant-scoped filesystem management with path traversal protection."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.core.schemas import AuditContext
from app.services.audit.schemas import AuditLogCreate
from app.services.audit.service import AuditService
from app.services.files.schemas import FileEntry, FileListResponse

logger = logging.getLogger(__name__)

# Characters that are safe in file/directory names across Linux, macOS, and Windows.
_SAFE_CHARS_RE = re.compile(r"[^\w\s\-.]")


def _secure_filename(name: str) -> str:
    """Return a filesystem-safe version of *name*.

    Strips path separators, leading dots/spaces, and any character that is
    not alphanumeric, a hyphen, an underscore, a space, or a period.
    An empty string is returned when no safe characters remain (callers must
    treat that as an invalid name).
    """
    # Collapse to basename so ``../evil`` becomes ``evil``
    name = os.path.basename(name)
    # Remove unsafe characters
    name = _SAFE_CHARS_RE.sub("", name)
    # Strip leading dots and whitespace (hidden-file or space-padding tricks)
    name = name.lstrip(". ")
    return name.strip()


class FilesService:
    """Tenant-scoped file manager.

    All paths are relative to ``<uploads_base_dir>/<tenant_slug>/``.
    Every mutating operation is audit-logged.
    """

    def __init__(self, uploads_base_dir: str, audit: AuditService) -> None:
        self._base = Path(uploads_base_dir).resolve()
        self._audit = audit

    # ── Internal helpers ──────────────────────────────────────────────

    def _safe_resolve(self, tenant_slug: str, rel_path: str) -> Path | None:
        """Resolve *rel_path* inside the tenant's directory.

        Returns the absolute :class:`Path` if it is safely inside the tenant
        root, or ``None`` if a path-traversal attack is detected.

        Protection layers:
        1. ``secure_filename`` is applied to the tenant slug and every path
           component of *rel_path* individually so that ``..`` and leading
           slashes are stripped.
        2. ``os.path.realpath`` resolves symlinks and removes ``..`` sequences
           on the final assembled path.
        3. An ``startswith`` prefix check ensures the result stays inside the
           tenant root.
        """
        safe_slug = _secure_filename(tenant_slug)
        if not safe_slug:
            return None

        tenant_root = (self._base / safe_slug).resolve()

        # Sanitize each component of the relative path separately so that
        # path separators introduced by secure_filename can't escape the root.
        safe_components: list[str] = []
        for component in Path(rel_path).parts:
            sanitized = _secure_filename(component)
            if sanitized:
                safe_components.append(sanitized)

        target = Path(os.path.realpath(tenant_root.joinpath(*safe_components))) if safe_components else tenant_root

        # Strict prefix check — target must be inside tenant_root
        try:
            target.relative_to(tenant_root)
        except ValueError:
            logger.warning(
                "Path traversal attempt blocked: slug=%s rel_path=%r resolved=%s",
                tenant_slug,
                rel_path,
                target,
            )
            return None

        return target

    def _ensure_tenant_dir(self, tenant_slug: str) -> Path:
        """Return the tenant root directory, creating it if needed."""
        safe_slug = _secure_filename(tenant_slug)
        if not safe_slug:
            raise ForbiddenError("Invalid tenant slug")
        tenant_root = self._base / safe_slug
        tenant_root.mkdir(parents=True, exist_ok=True)
        return tenant_root.resolve()

    @staticmethod
    def _entry_from_path(path: Path, tenant_root: Path) -> FileEntry:
        """Build a :class:`FileEntry` from a filesystem path."""
        stat = path.stat()
        rel = path.relative_to(tenant_root)
        is_dir = path.is_dir()
        return FileEntry(
            name=path.name,
            path=str(rel),
            type="folder" if is_dir else "file",
            size=0 if is_dir else stat.st_size,
            mtime=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            ext="" if is_dir else (path.suffix.lstrip(".").lower()),
        )

    # ── Public interface ──────────────────────────────────────────────

    async def list_files(self, tenant_slug: str, rel_path: str = "") -> FileListResponse:
        """List the contents of *rel_path* inside the tenant's upload directory.

        Folders are returned before files; both groups are sorted by name.
        """
        tenant_root = self._ensure_tenant_dir(tenant_slug)
        target = self._safe_resolve(tenant_slug, rel_path)
        if target is None:
            raise ForbiddenError("Path traversal detected")

        if not target.exists():
            raise NotFoundError("Directory", rel_path or "/")

        if not target.is_dir():
            raise ValidationError(f"Path '{rel_path}' is not a directory")

        raw_entries = list(target.iterdir())
        folders = sorted(
            [e for e in raw_entries if e.is_dir()],
            key=lambda p: p.name.lower(),
        )
        files = sorted(
            [e for e in raw_entries if e.is_file()],
            key=lambda p: p.name.lower(),
        )

        entries = [self._entry_from_path(p, tenant_root) for p in folders + files]

        return FileListResponse(
            ok=True,
            entries=entries,
            path=str(Path(rel_path)) if rel_path else "",
            tenant_slug=tenant_slug,
        )

    async def mkdir(
        self,
        tenant_slug: str,
        path: str,
        name: str,
        ctx: AuditContext,
    ) -> FileEntry:
        """Create a new directory named *name* inside *path*.

        Returns the :class:`FileEntry` for the newly created directory.
        """
        safe_name = _secure_filename(name)
        if not safe_name:
            raise ValidationError("Invalid directory name")

        parent = self._safe_resolve(tenant_slug, path)
        if parent is None:
            raise ForbiddenError("Path traversal detected")

        if not parent.exists():
            raise NotFoundError("Parent directory", path or "/")

        new_dir = parent / safe_name
        if new_dir.exists():
            raise ValidationError(f"'{safe_name}' already exists")

        new_dir.mkdir()

        tenant_root = self._ensure_tenant_dir(tenant_slug)
        entry = self._entry_from_path(new_dir, tenant_root)

        await self._audit.log(AuditLogCreate(
            user_id=ctx.user_id,
            tenant_id=ctx.tenant_id,
            action="file.mkdir",
            target_type="directory",
            target_id=str(entry.path),
            detail={"tenant_slug": tenant_slug, "path": path, "name": safe_name},
            ip_address=ctx.ip_address,
        ))

        return entry

    async def rename(
        self,
        tenant_slug: str,
        path: str,
        new_name: str,
        ctx: AuditContext,
    ) -> FileEntry:
        """Rename the item at *path* to *new_name* (same parent directory).

        Returns the :class:`FileEntry` for the renamed item.
        """
        safe_new_name = _secure_filename(new_name)
        if not safe_new_name:
            raise ValidationError("Invalid new name")

        target = self._safe_resolve(tenant_slug, path)
        if target is None:
            raise ForbiddenError("Path traversal detected")

        if not target.exists():
            raise NotFoundError("File or directory", path)

        tenant_root = self._ensure_tenant_dir(tenant_slug)
        # Guard: do not allow renaming the tenant root itself
        if target == tenant_root:
            raise ForbiddenError("Cannot rename the tenant root directory")

        dest = target.parent / safe_new_name
        # Validate destination is still inside the tenant root
        try:
            dest.resolve().relative_to(tenant_root)
        except ValueError:
            raise ForbiddenError("Path traversal detected in destination")

        if dest.exists():
            raise ValidationError(f"'{safe_new_name}' already exists")

        target.rename(dest)
        entry = self._entry_from_path(dest, tenant_root)

        await self._audit.log(AuditLogCreate(
            user_id=ctx.user_id,
            tenant_id=ctx.tenant_id,
            action="file.rename",
            target_type="file" if dest.is_file() else "directory",
            target_id=str(entry.path),
            detail={"tenant_slug": tenant_slug, "old_path": path, "new_name": safe_new_name},
            ip_address=ctx.ip_address,
        ))

        return entry

    async def delete(
        self,
        tenant_slug: str,
        path: str,
        ctx: AuditContext,
    ) -> None:
        """Delete the file or empty directory at *path*.

        Raises :class:`ForbiddenError` if *path* resolves to the tenant root.
        Raises :class:`ValidationError` if the directory is not empty.
        """
        target = self._safe_resolve(tenant_slug, path)
        if target is None:
            raise ForbiddenError("Path traversal detected")

        tenant_root = self._ensure_tenant_dir(tenant_slug)

        # Guard: never delete the tenant root
        if target == tenant_root:
            raise ForbiddenError("Cannot delete the tenant root directory")

        if not target.exists():
            raise NotFoundError("File or directory", path)

        if target.is_dir():
            # Only delete empty directories; use shutil.rmtree for recursive
            # deletes only if explicitly needed in future versions.
            if any(target.iterdir()):
                raise ValidationError("Directory is not empty; remove contents first")
            target.rmdir()
            entry_type = "directory"
        else:
            target.unlink()
            entry_type = "file"

        await self._audit.log(AuditLogCreate(
            user_id=ctx.user_id,
            tenant_id=ctx.tenant_id,
            action="file.delete",
            target_type=entry_type,
            target_id=path,
            detail={"tenant_slug": tenant_slug, "path": path},
            ip_address=ctx.ip_address,
        ))
