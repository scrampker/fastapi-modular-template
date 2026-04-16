"""SyncService — business logic for the mesh sync subsystem.

Responsible for:
  - Maintaining the local PlatformNode singleton.
  - CRUD on SyncPeer, SyncTenantSubscription, SyncConflict, PeerApiKey.
  - Issuing and rotating peer API keys (bcrypt prefix pattern).
  - Providing status snapshots consumed by the admin API.

MeshSyncEngine is the lifecycle object that owns async tasks; it is NOT
instantiated here.  The service is a pure stateless helper that the engine
and HTTP routes can call without knowing about each other.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scottycore.core.exceptions import AuthenticationError, ConflictError, NotFoundError
from scottycore.services.audit.service import AuditService
from scottycore.services.sync import auth as sync_auth
from scottycore.services.sync import repository as repo
from scottycore.services.sync import transport as tx
from scottycore.services.sync.models import (
    PlatformNode,
    SyncConflict,
    SyncPeer,
    SyncTenantSubscription,
)
from scottycore.services.sync.schemas import (
    NodeInfo,
    PeerCreateResponse,
    PeerSyncStatus,
    SyncConflictRead,
    SyncConflictResolution,
    SyncPeerCreate,
    SyncPeerRead,
    SyncPeerUpdate,
    SyncStatus,
    SyncTenantSubscriptionCreate,
    SyncTenantSubscriptionRead,
)

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_CAPABILITIES = ["delta_export", "lww_conflict"]

# ── Vault-based encryption helpers for their_key_for_us ──────────────────────
# We encode as: "vault:<hex_nonce>:<hex_ciphertext>" so plain-text keys
# (lacking the prefix) are still accepted during migration.

_VAULT_PREFIX = "vault:"


def _encrypt_peer_key(plaintext: str, vault_key: bytes) -> str:
    """AES-256-GCM encrypt a peer key using the supplied vault session key."""
    import os
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    ct = AESGCM(vault_key).encrypt(nonce, plaintext.encode(), None)
    return f"{_VAULT_PREFIX}{nonce.hex()}:{ct.hex()}"


def _decrypt_peer_key(stored: str, vault_key: bytes) -> str:
    """Decrypt a vault-encoded peer key."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _, rest = stored.split(":", 1)
    nonce_hex, ct_hex = rest.split(":", 1)
    pt = AESGCM(vault_key).decrypt(bytes.fromhex(nonce_hex), bytes.fromhex(ct_hex), None)
    return pt.decode()


def _get_vault_key() -> bytes | None:
    """Return the active vault session key, or None if vault is not configured."""
    try:
        from pathlib import Path
        from app.services.vault.crypto import get_session_key
        # project_root is two levels above the scottycore package directory.
        import scottycore
        project_root = Path(scottycore.__file__).parent.parent
        return get_session_key(project_root)
    except Exception:
        return None


def encrypt_their_key(plaintext: str) -> str:
    """Wrap their_key_for_us in vault encryption if vault is available."""
    key = _get_vault_key()
    if key is None:
        logger.warning(
            "vault not configured; peer key stored plaintext — "
            "initialise vault to enable encryption at rest"
        )
        return plaintext
    return _encrypt_peer_key(plaintext, key)


def decrypt_their_key(stored: str | None) -> str | None:
    """Unwrap their_key_for_us; handles both vault-encoded and plaintext forms."""
    if stored is None:
        return None
    if not stored.startswith(_VAULT_PREFIX):
        return stored  # Legacy plaintext — returned as-is.
    key = _get_vault_key()
    if key is None:
        logger.warning(
            "vault not configured but stored key is vault-encoded; "
            "cannot decrypt — unlock the vault first"
        )
        return None
    try:
        return _decrypt_peer_key(stored, key)
    except Exception as exc:
        logger.error("Failed to decrypt peer key: %s", exc)
        return None


async def migrate_plaintext_peer_keys(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """One-time migration: re-encrypt plaintext their_key_for_us values.

    Call this after vault comes online.  Returns the count of keys updated.
    """
    key = _get_vault_key()
    if key is None:
        logger.warning("Skipping peer key migration — vault not unlocked")
        return 0

    updated = 0
    async with session_factory() as session:
        from sqlalchemy import select
        from scottycore.services.sync.models import SyncPeer as _SyncPeer
        peers = list((await session.scalars(select(_SyncPeer))).all())
        for peer in peers:
            if peer.their_key_for_us and not peer.their_key_for_us.startswith(_VAULT_PREFIX):
                peer.their_key_for_us = _encrypt_peer_key(peer.their_key_for_us, key)
                updated += 1
        if updated:
            await session.commit()
    logger.info("Migrated %d plaintext peer keys to vault encryption", updated)
    return updated


def _peer_to_read(peer: SyncPeer) -> SyncPeerRead:
    return SyncPeerRead(
        id=peer.id,
        peer_node_id=peer.peer_node_id,
        name=peer.name,
        base_url=peer.base_url,
        has_their_key=bool(peer.their_key_for_us),
        our_key_for_them_prefix=peer.our_key_for_them_prefix,
        enabled=peer.enabled,
        sync_mode=peer.sync_mode,  # type: ignore[arg-type]
        auto_include_new_tenants=peer.auto_include_new_tenants,
        last_pulled_ts=peer.last_pulled_ts,
        last_pushed_ts=peer.last_pushed_ts,
        last_error=peer.last_error,
        last_error_ts=peer.last_error_ts,
        backoff_seconds=peer.backoff_seconds,
        created_at=peer.created_at,
        updated_at=peer.updated_at,
    )


def _conflict_to_read(c: SyncConflict) -> SyncConflictRead:
    def _try_json(s: str | None) -> dict[str, Any] | None:
        if s is None:
            return None
        try:
            return json.loads(s)
        except Exception:
            return None

    return SyncConflictRead(
        id=c.id,
        peer_id=c.peer_id,
        tenant_id=c.tenant_id,
        resource_type=c.resource_type,
        resource_id=c.resource_id,
        local_payload=_try_json(c.local_payload),
        remote_payload=_try_json(c.remote_payload),
        detected_at=c.detected_at,
        resolved_at=c.resolved_at,
        resolved_by=c.resolved_by,
        resolution=c.resolution,
    )


class SyncService:
    """Stateless coordinator for sync metadata.  Thread-safe; shares session factory."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        audit_service: AuditService,
        node_name: str | None = None,
    ) -> None:
        self._sf = session_factory
        self._audit = audit_service
        # Platform node name defaults to settings.app_name if not passed
        # explicitly. Consumer apps can override for multi-node deployments.
        if node_name is None:
            from scottycore.core.config import get_settings
            node_name = get_settings().app_name
        self._node_name = node_name

    # ── Local node ────────────────────────────────────────────────────────

    async def ensure_local_node(self) -> PlatformNode:
        """Create the PlatformNode singleton if it doesn't exist; return it.

        Uses INSERT … ON CONFLICT DO NOTHING (Postgres) / INSERT OR IGNORE (SQLite)
        via repo.create_platform_node_idempotent() to eliminate the SELECT-then-INSERT
        race that could produce duplicate rows under concurrent startup.
        """
        await repo.create_platform_node_idempotent(self._sf, node_name=self._node_name)
        async with self._sf() as session:
            node = await repo.get_platform_node(session)
        if node is None:
            raise RuntimeError("Failed to create PlatformNode — check DB connectivity")
        return node

    async def get_local_node_info(self) -> NodeInfo:
        async with self._sf() as session:
            node = await repo.get_platform_node(session)
        if node is None:
            raise RuntimeError("PlatformNode not initialised — call ensure_local_node() first")
        return NodeInfo(
            node_id=node.id,
            node_name=node.node_name,
            schema_version=_SCHEMA_VERSION,
            capabilities=list(_CAPABILITIES),
        )

    # ── Peer CRUD ─────────────────────────────────────────────────────────

    async def create_peer(self, data: SyncPeerCreate) -> PeerCreateResponse:
        """Create a new peer, issue an API key for them, return plaintext ONCE."""
        plaintext, key_hash, key_prefix = sync_auth.generate_peer_key()

        # Encrypt their_key_for_us at rest using vault if available.
        stored_their_key = (
            encrypt_their_key(data.their_key_for_us)
            if data.their_key_for_us
            else None
        )

        async with self._sf() as session:
            peer = await repo.create_peer(
                session,
                name=data.name,
                base_url=data.base_url,
                their_key_for_us=stored_their_key,
                our_key_for_them_hash=key_hash,
                our_key_for_them_prefix=key_prefix,
                enabled=data.enabled,
                sync_mode=data.sync_mode.value,
                auto_include_new_tenants=data.auto_include_new_tenants,
            )
            # Also create a PeerApiKey row for the prefix-lookup path.
            await repo.create_peer_api_key(session, peer.id, key_hash, key_prefix)
            await session.commit()
            await session.refresh(peer)

        return PeerCreateResponse(
            peer=_peer_to_read(peer),
            our_key_for_them_plaintext=plaintext,
        )

    async def list_peers(self) -> list[SyncPeerRead]:
        async with self._sf() as session:
            peers = await repo.list_peers(session)
        return [_peer_to_read(p) for p in peers]

    async def get_peer(self, peer_id: str) -> SyncPeerRead:
        async with self._sf() as session:
            peer = await repo.get_peer(session, peer_id)
        if peer is None:
            raise NotFoundError("SyncPeer", peer_id)
        return _peer_to_read(peer)

    async def update_peer(self, peer_id: str, data: SyncPeerUpdate) -> SyncPeerRead:
        fields = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
        # Coerce SyncMode enum to its string value if present.
        if "sync_mode" in fields and hasattr(fields["sync_mode"], "value"):
            fields["sync_mode"] = fields["sync_mode"].value
        # Encrypt their_key_for_us if being updated.
        if "their_key_for_us" in fields and fields["their_key_for_us"]:
            fields["their_key_for_us"] = encrypt_their_key(fields["their_key_for_us"])
        async with self._sf() as session:
            peer = await repo.update_peer_fields(session, peer_id, fields)
            if peer is None:
                raise NotFoundError("SyncPeer", peer_id)
            await session.commit()
            await session.refresh(peer)
        return _peer_to_read(peer)

    async def delete_peer(self, peer_id: str) -> None:
        async with self._sf() as session:
            peer = await repo.get_peer(session, peer_id)
            if peer is None:
                raise NotFoundError("SyncPeer", peer_id)
            await session.delete(peer)
            await session.commit()

    async def rotate_peer_key(self, peer_id: str) -> str:
        """Rotate the API key for ``peer_id``.

        Returns the new plaintext key.  Caller must transmit it to the peer
        out-of-band before the next sync cycle.
        """
        plaintext, key_hash, key_prefix = sync_auth.generate_peer_key()
        now = datetime.now(timezone.utc)

        async with self._sf() as session:
            peer = await repo.get_peer(session, peer_id)
            if peer is None:
                raise NotFoundError("SyncPeer", peer_id)
            # Deactivate old PeerApiKey rows, create a new one.
            await repo.deactivate_peer_keys(session, peer_id, rotated_at=now)
            await repo.create_peer_api_key(session, peer_id, key_hash, key_prefix)
            # Update SyncPeer so the prefix column stays in sync for fast reads.
            await repo.update_peer_fields(
                session,
                peer_id,
                {"our_key_for_them_hash": key_hash, "our_key_for_them_prefix": key_prefix},
            )
            await session.commit()

        return plaintext

    async def probe_peer(self, peer_id: str) -> NodeInfo:
        """Call the peer's /sync/info endpoint and update peer_node_id if changed."""
        async with self._sf() as session:
            peer = await repo.get_peer(session, peer_id)
        if peer is None:
            raise NotFoundError("SyncPeer", peer_id)
        if not peer.their_key_for_us:
            raise ConflictError("Cannot probe peer — their_key_for_us not configured")

        info = await tx.call_peer_info(peer.base_url, peer.their_key_for_us)

        if peer.peer_node_id != info.node_id:
            async with self._sf() as session:
                await repo.update_peer_fields(session, peer_id, {"peer_node_id": info.node_id})
                await session.commit()

        return info

    # ── Tenant subscriptions ──────────────────────────────────────────────

    async def list_tenant_subs(self, peer_id: str) -> list[SyncTenantSubscriptionRead]:
        async with self._sf() as session:
            subs = await repo.list_tenant_subs(session, peer_id)
        return [SyncTenantSubscriptionRead.model_validate(s) for s in subs]

    async def create_tenant_sub(
        self, peer_id: str, data: SyncTenantSubscriptionCreate
    ) -> SyncTenantSubscriptionRead:
        async with self._sf() as session:
            sub = await repo.create_tenant_sub(
                session, peer_id, data.tenant_id, data.direction.value
            )
            await session.commit()
            await session.refresh(sub)
        return SyncTenantSubscriptionRead.model_validate(sub)

    async def delete_tenant_sub(self, sub_id: str) -> None:
        async with self._sf() as session:
            deleted = await repo.delete_tenant_sub(session, sub_id)
            if not deleted:
                raise NotFoundError("SyncTenantSubscription", sub_id)
            await session.commit()

    # ── Conflicts ─────────────────────────────────────────────────────────

    async def list_conflicts(
        self,
        peer_id: str | None = None,
        unresolved_only: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SyncConflictRead]:
        async with self._sf() as session:
            conflicts = await repo.list_conflicts(
                session, peer_id=peer_id, unresolved_only=unresolved_only,
                limit=limit, offset=offset
            )
        return [_conflict_to_read(c) for c in conflicts]

    async def resolve_conflict(
        self, conflict_id: str, data: SyncConflictResolution
    ) -> SyncConflictRead:
        async with self._sf() as session:
            conflict = await repo.resolve_conflict(
                session, conflict_id, data.resolution, data.resolved_by
            )
            if conflict is None:
                raise NotFoundError("SyncConflict", conflict_id)
            await session.commit()
            await session.refresh(conflict)
        return _conflict_to_read(conflict)

    # ── Row origin (echo prevention) ──────────────────────────────────────

    async def record_row_origin(
        self, table_name: str, row_id: str, origin_node_id: str
    ) -> None:
        """Called by the engine during restore to track where each row came from."""
        async with self._sf() as session:
            await repo.upsert_row_origin(session, table_name, row_id, origin_node_id)
            await session.commit()

    # ── Status snapshot ───────────────────────────────────────────────────

    async def status_summary(self, running_peer_ids: set[str]) -> SyncStatus:
        """Return a per-peer status snapshot.

        ``running_peer_ids`` is supplied by the engine at call time (it knows
        which asyncio tasks are alive).
        """
        async with self._sf() as session:
            node = await repo.get_platform_node(session)
            peers = await repo.list_peers(session)

        node_id = node.id if node else "unknown"
        node_name = node.node_name if node else "unknown"

        peer_statuses = [
            PeerSyncStatus(
                peer_id=p.id,
                peer_name=p.name,
                enabled=p.enabled,
                last_pulled_ts=p.last_pulled_ts,
                last_pushed_ts=p.last_pushed_ts,
                last_error=p.last_error,
                backoff_seconds=p.backoff_seconds,
                is_running=p.id in running_peer_ids,
            )
            for p in peers
        ]
        return SyncStatus(
            local_node_id=node_id,
            local_node_name=node_name,
            peers=peer_statuses,
        )

    # ── Peer auth helper (used by HTTP dependency) ────────────────────────

    async def authenticate_peer_bearer(self, bearer: str) -> SyncPeer:
        """Resolve a Bearer token to the owning SyncPeer.  Raises AuthenticationError."""
        async with self._sf() as session:
            return await sync_auth.authenticate_peer(bearer, session)
