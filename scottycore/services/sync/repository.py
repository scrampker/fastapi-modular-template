"""Async DB helpers for the sync subsystem.

All methods accept an open AsyncSession and return ORM objects or None.
Callers are responsible for committing/rolling back.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scottycore.services.sync.models import (
    PeerApiKey,
    PlatformNode,
    SyncConflict,
    SyncPeer,
    SyncRowOrigin,
    SyncTenantSubscription,
)

logger = logging.getLogger(__name__)


# ── PlatformNode ───────────────────────────────────────────────────────────


async def get_platform_node(session: AsyncSession) -> PlatformNode | None:
    result = await session.scalars(select(PlatformNode).limit(1))
    return result.first()


async def create_platform_node(
    session: AsyncSession, node_name: str
) -> PlatformNode:
    node = PlatformNode(node_name=node_name)
    session.add(node)
    await session.flush()
    return node


async def create_platform_node_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
    node_name: str,
) -> None:
    """Create the PlatformNode singleton in a race-free way.

    Uses a fixed sentinel PK ``"singleton"`` so that the PRIMARY KEY constraint
    itself prevents duplicates.  ``INSERT OR IGNORE`` (SQLite) /
    ``INSERT … ON CONFLICT DO NOTHING`` (PostgreSQL) means that concurrent
    calls at startup silently no-op after the first one succeeds.

    Callers must follow up with ``get_platform_node()`` to retrieve the node —
    this function intentionally returns nothing to keep the interface simple.
    """
    # Fixed sentinel PK so the PRIMARY KEY constraint itself prevents duplicates.
    _SINGLETON_ID = "singleton"

    async with session_factory() as session:
        dialect = session.bind.dialect.name if session.bind else "unknown"  # type: ignore[union-attr]
        if dialect == "postgresql":
            stmt = text(
                "INSERT INTO platform_node (id, node_name) "
                "VALUES (:id, :node_name) "
                "ON CONFLICT DO NOTHING"
            )
        else:
            # SQLite and fallback
            stmt = text(
                "INSERT OR IGNORE INTO platform_node (id, node_name) "
                "VALUES (:id, :node_name)"
            )
        await session.execute(stmt, {"id": _SINGLETON_ID, "node_name": node_name})
        await session.commit()


# ── SyncPeer ───────────────────────────────────────────────────────────────


async def get_peer(session: AsyncSession, peer_id: str) -> SyncPeer | None:
    return await session.get(SyncPeer, peer_id)


async def list_peers(session: AsyncSession) -> list[SyncPeer]:
    result = await session.scalars(select(SyncPeer).order_by(SyncPeer.created_at))
    return list(result.all())


async def list_enabled_peers(session: AsyncSession) -> list[SyncPeer]:
    result = await session.scalars(
        select(SyncPeer).where(SyncPeer.enabled.is_(True)).order_by(SyncPeer.created_at)
    )
    return list(result.all())


async def create_peer(session: AsyncSession, **kwargs: Any) -> SyncPeer:
    peer = SyncPeer(**kwargs)
    session.add(peer)
    await session.flush()
    return peer


async def update_peer_fields(
    session: AsyncSession, peer_id: str, fields: dict[str, Any]
) -> SyncPeer | None:
    peer = await get_peer(session, peer_id)
    if peer is None:
        return None
    for k, v in fields.items():
        setattr(peer, k, v)
    await session.flush()
    return peer


async def record_pull_success(
    session: AsyncSession, peer_id: str, ts: datetime
) -> None:
    await session.execute(
        update(SyncPeer)
        .where(SyncPeer.id == peer_id)
        .values(last_pulled_ts=ts, last_error=None, last_error_ts=None, backoff_seconds=0)
    )


async def record_push_success(
    session: AsyncSession, peer_id: str, ts: datetime
) -> None:
    await session.execute(
        update(SyncPeer)
        .where(SyncPeer.id == peer_id)
        .values(last_pushed_ts=ts, last_error=None, last_error_ts=None, backoff_seconds=0)
    )


async def record_peer_error(
    session: AsyncSession, peer_id: str, error: str, backoff: int
) -> None:
    now = datetime.now(timezone.utc)
    await session.execute(
        update(SyncPeer)
        .where(SyncPeer.id == peer_id)
        .values(last_error=error, last_error_ts=now, backoff_seconds=backoff)
    )


# ── SyncTenantSubscription ─────────────────────────────────────────────────


async def list_tenant_subs(
    session: AsyncSession, peer_id: str
) -> list[SyncTenantSubscription]:
    result = await session.scalars(
        select(SyncTenantSubscription)
        .where(SyncTenantSubscription.peer_id == peer_id)
        .order_by(SyncTenantSubscription.created_at)
    )
    return list(result.all())


async def create_tenant_sub(
    session: AsyncSession, peer_id: str, tenant_id: str, direction: str
) -> SyncTenantSubscription:
    sub = SyncTenantSubscription(peer_id=peer_id, tenant_id=tenant_id, direction=direction)
    session.add(sub)
    await session.flush()
    return sub


async def delete_tenant_sub(session: AsyncSession, sub_id: str) -> bool:
    sub = await session.get(SyncTenantSubscription, sub_id)
    if sub is None:
        return False
    await session.delete(sub)
    await session.flush()
    return True


# ── SyncConflict ───────────────────────────────────────────────────────────


async def create_conflict(session: AsyncSession, **kwargs: Any) -> SyncConflict:
    conflict = SyncConflict(**kwargs)
    session.add(conflict)
    await session.flush()
    return conflict


async def list_conflicts(
    session: AsyncSession,
    peer_id: str | None = None,
    unresolved_only: bool = True,
    limit: int = 100,
    offset: int = 0,
) -> list[SyncConflict]:
    q = select(SyncConflict).order_by(SyncConflict.detected_at.desc())
    if peer_id is not None:
        q = q.where(SyncConflict.peer_id == peer_id)
    if unresolved_only:
        q = q.where(SyncConflict.resolved_at.is_(None))
    q = q.limit(limit).offset(offset)
    result = await session.scalars(q)
    return list(result.all())


async def resolve_conflict(
    session: AsyncSession,
    conflict_id: str,
    resolution: str,
    resolved_by: str,
) -> SyncConflict | None:
    conflict = await session.get(SyncConflict, conflict_id)
    if conflict is None:
        return None
    conflict.resolved_at = datetime.now(timezone.utc)
    conflict.resolved_by = resolved_by
    conflict.resolution = resolution
    await session.flush()
    return conflict


# ── PeerApiKey ─────────────────────────────────────────────────────────────


async def get_active_key_by_prefix(
    session: AsyncSession, prefix: str
) -> PeerApiKey | None:
    """O(1) candidate lookup by prefix; caller must bcrypt-verify the match."""
    result = await session.scalars(
        select(PeerApiKey)
        .where(PeerApiKey.key_prefix == prefix, PeerApiKey.is_active.is_(True))
        .limit(1)
    )
    return result.first()


async def create_peer_api_key(
    session: AsyncSession, peer_id: str, key_hash: str, key_prefix: str
) -> PeerApiKey:
    key = PeerApiKey(peer_id=peer_id, key_hash=key_hash, key_prefix=key_prefix)
    session.add(key)
    await session.flush()
    return key


async def deactivate_peer_keys(
    session: AsyncSession, peer_id: str, rotated_at: datetime
) -> None:
    """Mark all active keys for a peer as inactive (rotation step)."""
    await session.execute(
        update(PeerApiKey)
        .where(PeerApiKey.peer_id == peer_id, PeerApiKey.is_active.is_(True))
        .values(is_active=False, rotated_at=rotated_at)
    )


# ── SyncRowOrigin ──────────────────────────────────────────────────────────


async def upsert_row_origin(
    session: AsyncSession,
    table_name: str,
    row_id: str,
    origin_node_id: str,
) -> None:
    """Record (or update) the origin node for a synced row.

    Uses merge() so it works as upsert on the composite PK.
    """
    obj = SyncRowOrigin(
        table_name=table_name,
        row_id=row_id,
        origin_node_id=origin_node_id,
        last_synced_ts=datetime.now(timezone.utc),
    )
    await session.merge(obj)


async def get_row_origin(
    session: AsyncSession, table_name: str, row_id: str
) -> SyncRowOrigin | None:
    return await session.get(SyncRowOrigin, (table_name, row_id))
