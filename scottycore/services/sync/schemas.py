"""Pydantic schemas for the mesh sync subsystem."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ── Enums ──────────────────────────────────────────────────────────────────


class SyncMode(str, Enum):
    """Determines which tenants/data are included in a sync cycle with a peer."""

    FULL = "full"
    TENANTS_ONLY = "tenants_only"
    SELECTED = "selected"


class SyncDirection(str, Enum):
    """Which direction data flows for a tenant subscription."""

    IN = "in"
    OUT = "out"
    BOTH = "both"


# ── Node info ──────────────────────────────────────────────────────────────


class NodeInfo(BaseModel):
    """Public identity of a node, returned by GET /sync/info."""

    node_id: str
    node_name: str
    schema_version: int = 1
    capabilities: list[str] = Field(default_factory=list)


# ── Peer CRUD ──────────────────────────────────────────────────────────────


class SyncPeerCreate(BaseModel):
    name: str
    base_url: str
    their_key_for_us: str | None = None
    sync_mode: SyncMode = SyncMode.FULL
    auto_include_new_tenants: bool = True
    enabled: bool = True


class SyncPeerUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    their_key_for_us: str | None = None
    sync_mode: SyncMode | None = None
    auto_include_new_tenants: bool | None = None
    enabled: bool | None = None


class SyncPeerRead(BaseModel):
    """Peer read model.  our_key_for_them is NEVER returned; only the prefix."""

    id: str
    peer_node_id: str | None
    name: str
    base_url: str
    # their_key_for_us is sensitive — redacted in reads.
    has_their_key: bool
    our_key_for_them_prefix: str | None
    enabled: bool
    sync_mode: SyncMode
    auto_include_new_tenants: bool
    last_pulled_ts: datetime | None
    last_pushed_ts: datetime | None
    last_error: str | None
    last_error_ts: datetime | None
    backoff_seconds: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PeerCreateResponse(BaseModel):
    """Response to creating a new peer.

    ``our_key_for_them_plaintext`` is the API key this node will accept from
    the peer.  It is shown ONCE and never stored in plaintext — the caller
    must copy it to the peer's ``their_key_for_us`` configuration.
    """

    peer: SyncPeerRead
    our_key_for_them_plaintext: str


# ── Tenant subscriptions ───────────────────────────────────────────────────


class SyncTenantSubscriptionCreate(BaseModel):
    tenant_id: str
    direction: SyncDirection = SyncDirection.BOTH


class SyncTenantSubscriptionRead(BaseModel):
    id: str
    peer_id: str
    tenant_id: str
    direction: SyncDirection
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Conflicts ──────────────────────────────────────────────────────────────


class SyncConflictRead(BaseModel):
    id: str
    peer_id: str
    tenant_id: str | None
    resource_type: str
    resource_id: str
    local_payload: dict[str, Any] | None
    remote_payload: dict[str, Any] | None
    detected_at: datetime
    resolved_at: datetime | None
    resolved_by: str | None
    resolution: str | None

    model_config = {"from_attributes": True}


class SyncConflictResolution(BaseModel):
    resolution: str  # e.g. "accept_local", "accept_remote", "manual"
    resolved_by: str


# ── Wire protocol ──────────────────────────────────────────────────────────


class PullRequest(BaseModel):
    """Body sent to a peer's POST /sync/pull endpoint."""

    # Only include rows updated after this timestamp.  None means full export.
    since: datetime | None = None
    # Scope dict: contributor_ids and/or tenant_ids to include.
    scope: dict[str, Any] = Field(default_factory=dict)
    # Peer should exclude rows whose origin matches any of these node IDs.
    # Callers pass [our_node_id] to prevent echo.
    exclude_origins: list[str] = Field(default_factory=list)


class PushRequest(BaseModel):
    """Metadata accompanying a POST /sync/push multipart upload."""

    origin_node_id: str


class PushResponse(BaseModel):
    """Returned by POST /sync/push after applying a received bundle."""

    rows_applied: int
    rows_skipped: int
    conflicts_created: int
    # The peer's last pull timestamp so the caller can track progress.
    peer_last_pulled_ts: datetime | None


# ── Status ─────────────────────────────────────────────────────────────────


class PeerSyncStatus(BaseModel):
    """Per-peer sync health snapshot returned by GET /admin/sync/status."""

    peer_id: str
    peer_name: str
    enabled: bool
    last_pulled_ts: datetime | None
    last_pushed_ts: datetime | None
    last_error: str | None
    backoff_seconds: int
    is_running: bool


class SyncStatus(BaseModel):
    local_node_id: str
    local_node_name: str
    peers: list[PeerSyncStatus]
