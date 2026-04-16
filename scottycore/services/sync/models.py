"""SQLAlchemy ORM models for the mesh sync subsystem.

NOTE: A proper Alembic migration should be generated before deploying to
production.  In dev, scottycore's lifespan imports this module so that
Base.metadata.create_all picks up these tables automatically.

Tables:
  platform_node         — single-row singleton identifying this installation.
  sync_peers            — known peer nodes.
  sync_tenant_subs      — per-peer tenant subscription overrides (SELECTED mode).
  sync_conflicts        — LWW conflict records written when a remote row loses.
  peer_api_keys         — rotatable API key credentials issued to peers.
  sync_row_origin       — echo-prevention sidecar: tracks which node originated
                          each synced row so we can skip re-pushing it back.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from scottycore.core.database import Base, new_uuid


class PlatformNode(Base):
    """Single-row singleton that identifies this installation in the mesh.

    Created by SyncService.ensure_local_node() on first boot.  There must never
    be more than one row in this table — the service enforces that by checking
    before inserting.
    """

    __tablename__ = "platform_node"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    node_name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Immutable installation timestamp — not updated on restart.
    install_ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class SyncPeer(Base):
    """A known peer node in the mesh.

    ``peer_node_id`` is None until the first successful /sync/info handshake
    confirms what node_id the remote reports.

    ``their_key_for_us`` stores the raw API key the peer issued for us to
    authenticate against their /sync/* endpoints.
    TODO: encrypt this column using scottycore vault before shipping to the
    shared framework.  For MVP the plaintext value is acceptable because
    it never leaves the database row.

    ``sync_mode`` is one of: "full", "tenants_only", "selected".
    """

    __tablename__ = "sync_peers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    peer_node_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)

    # Key WE hold to call THEIR /sync/* endpoints.
    # TODO: encrypt with scottycore vault integration.
    their_key_for_us: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Hash+prefix of OUR key that THEY use to call our /sync/* endpoints.
    # Stored as bcrypt hash; prefix enables O(1) candidate lookup before bcrypt.
    our_key_for_them_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    our_key_for_them_prefix: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # "full" | "tenants_only" | "selected"
    sync_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="full")

    # Only meaningful when sync_mode != "full"; greyed-out TRUE when mode=full.
    auto_include_new_tenants: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    last_pulled_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_pushed_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    backoff_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SyncTenantSubscription(Base):
    """Which tenants are included/excluded for a given peer (SELECTED mode).

    Only consulted when ``SyncPeer.sync_mode == "selected"``.  In "full" mode
    all tenants are synced; in "tenants_only" mode only tenants that we and
    the peer share are synced.

    ``direction`` is one of: "in", "out", "both".
    """

    __tablename__ = "sync_tenant_subs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    peer_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sync_peers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # "in" | "out" | "both"
    direction: Mapped[str] = mapped_column(String(10), nullable=False, default="both")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SyncConflict(Base):
    """Records a Last-Write-Wins conflict where the remote row was not applied.

    Conflicts arise when two nodes update the same row and the remote version
    loses on ``updated_at`` comparison (ties broken by origin_node_id lex).
    The losing payload is stored here for manual review or automated resolution.

    In ``tenants_only`` mode a tenant user-access mismatch is also recorded here
    with resolution = "access_blocked".
    """

    __tablename__ = "sync_conflicts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    peer_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sync_peers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(200), nullable=False)
    # JSON blobs stored as text; large payloads should be truncated by callers.
    local_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    remote_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(100), nullable=True)


class PeerApiKey(Base):
    """Rotatable API keys issued to peer nodes.

    Each peer can have at most one active key at a time (is_active=True).
    Old keys are retained for audit purposes with is_active=False.

    Stored as bcrypt hash + prefix for O(1) candidate lookup (same pattern
    as Tenant.api_key_hash / api_key_prefix).
    """

    __tablename__ = "peer_api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    peer_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sync_peers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SyncRowOrigin(Base):
    """Echo-prevention sidecar: records the origin node for each synced row.

    When we receive a delta bundle from peer P, we record every applied row's
    origin_node_id here.  On the next pull request we pass
    ``exclude_origins=[our_node_id]`` so peers don't re-send rows that
    originated here.

    Composite PK on (table_name, row_id) — only the most recent origin is
    retained.  last_synced_ts is the wall-clock of the last sync cycle that
    touched this row.
    """

    __tablename__ = "sync_row_origin"

    table_name: Mapped[str] = mapped_column(String(100), primary_key=True)
    row_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    origin_node_id: Mapped[str] = mapped_column(String(36), nullable=False)
    last_synced_ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_sync_row_origin_origin", "origin_node_id"),
    )
