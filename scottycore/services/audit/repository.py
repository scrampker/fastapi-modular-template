"""PRIVATE: Audit log database operations."""

from __future__ import annotations

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from scottycore.services.audit.models import AuditLog
from scottycore.services.audit.schemas import AuditLogCreate, AuditLogFilter


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: AuditLogCreate) -> AuditLog:
        record = AuditLog(
            tenant_id=str(data.tenant_id) if data.tenant_id else None,
            user_id=str(data.user_id),
            action=data.action,
            target_type=data.target_type,
            target_id=data.target_id,
            detail=data.detail,
            ip_address=data.ip_address,
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def list(
        self, tenant_id: str | None, filters: AuditLogFilter
    ) -> tuple[list[AuditLog], int]:
        query = select(AuditLog)
        count_query = select(func.count(AuditLog.id))

        if tenant_id is not None:
            query = query.where(AuditLog.tenant_id == tenant_id)
            count_query = count_query.where(AuditLog.tenant_id == tenant_id)

        if filters.user_id:
            query = query.where(AuditLog.user_id == str(filters.user_id))
            count_query = count_query.where(AuditLog.user_id == str(filters.user_id))
        if filters.action:
            # Support prefix matching: "auth" matches "auth.login", "auth.logout", etc.
            if "." in filters.action:
                query = query.where(AuditLog.action == filters.action)
                count_query = count_query.where(AuditLog.action == filters.action)
            else:
                prefix = filters.action + ".%"
                query = query.where(AuditLog.action.like(prefix))
                count_query = count_query.where(AuditLog.action.like(prefix))
        if filters.date_from:
            query = query.where(AuditLog.timestamp >= filters.date_from)
            count_query = count_query.where(AuditLog.timestamp >= filters.date_from)
        if filters.date_to:
            query = query.where(AuditLog.timestamp <= filters.date_to)
            count_query = count_query.where(AuditLog.timestamp <= filters.date_to)

        total = (await self._session.scalar(count_query)) or 0
        query = query.order_by(AuditLog.timestamp.desc())
        query = query.offset(filters.offset).limit(filters.per_page)
        result = await self._session.scalars(query)
        return list(result.all()), total
