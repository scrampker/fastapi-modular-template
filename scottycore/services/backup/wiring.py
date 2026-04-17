"""Factory that builds a BackupService with all built-in contributors.

Registration order is FK-safe: low-level tables (tenants, users) come before
their dependents (settings, items, audit). Consumer apps can register
additional domain contributors after calling :func:`build_backup_service`.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scottycore.services.audit.service import AuditService
from scottycore.services.backup.contributors import (
    AuditLogContributor,
    ItemsContributor,
    SettingsContributor,
    TenantsContributor,
    UsersContributor,
)
from scottycore.services.backup.service import BackupService


def build_backup_service(
    session_factory: async_sessionmaker[AsyncSession],
    audit_service: AuditService,
    *,
    app_name: str | None = None,
    app_version: str | None = None,
) -> BackupService:
    """Build a BackupService and pre-register the scottycore built-ins."""
    svc = BackupService(
        session_factory=session_factory,
        audit_service=audit_service,
        app_name=app_name,
        app_version=app_version,
    )
    # Order matters on restore — FK-safe insertion sequence.
    svc.register(TenantsContributor(session_factory))
    svc.register(UsersContributor(session_factory))
    svc.register(SettingsContributor(session_factory))
    svc.register(ItemsContributor(session_factory))
    svc.register(AuditLogContributor(session_factory))
    return svc
