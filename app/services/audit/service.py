"""Audit service — public interface for audit logging."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.core.schemas import PaginatedResponse
from app.services.audit.repository import AuditRepository
from app.services.audit.schemas import AuditLogCreate, AuditLogFilter, AuditLogRead


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
