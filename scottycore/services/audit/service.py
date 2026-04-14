"""Audit service — public interface for audit logging."""
# scottycore-pattern: audit.immutable_log
# scottycore-pattern: audit.phi_data_access

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from scottycore.core.schemas import PaginatedResponse
from scottycore.services.audit.repository import AuditRepository
from scottycore.services.audit.schemas import AuditLogCreate, AuditLogFilter, AuditLogRead


class AuditService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def log(self, entry: AuditLogCreate) -> None:
        """Write an audit log entry. Fire-and-forget — never fails the caller."""
        try:
            async with self._session_factory() as session:
                repo = AuditRepository(session)
                await repo.create(entry)
                await session.commit()
        except Exception:
            # Audit logging must never break the calling operation.
            # In production, this should go to stderr or a fallback log.
            pass

    async def log_data_access(
        self,
        user_id: UUID,
        resource_type: str,
        resource_id: str,
        action: str = "view",
        tenant_id: UUID | None = None,
        ip_address: str = "unknown",
    ) -> None:
        """Convenience helper for PHI / sensitive data access audit events.

        HIPAA-aligned modules that expose sensitive data MUST call this on
        every read that returns patient or otherwise sensitive records.

        Example::

            await self._audit.log_data_access(
                user_id=ctx.user_id,
                resource_type="item",
                resource_id=str(item.id),
                action="view",
                tenant_id=tenant_id,
                ip_address=ctx.ip_address,
            )

        The ``action`` value should use the format ``"<resource_type>.<verb>"``
        for consistency with the rest of the audit log (e.g. ``"item.view"``,
        ``"record.list"``).
        """
        await self.log(AuditLogCreate(
            user_id=user_id,
            tenant_id=tenant_id,
            action=f"{resource_type}.{action}",
            target_type=resource_type,
            target_id=resource_id,
            ip_address=ip_address,
        ))

    async def list_logs(
        self, tenant_id: UUID | None, filters: AuditLogFilter
    ) -> PaginatedResponse[AuditLogRead]:
        async with self._session_factory() as session:
            repo = AuditRepository(session)
            tid = str(tenant_id) if tenant_id else None
            records, total = await repo.list(tid, filters)
            return PaginatedResponse[AuditLogRead](
                items=[AuditLogRead.model_validate(r) for r in records],
                total=total,
                page=filters.page,
                per_page=filters.per_page,
            )
