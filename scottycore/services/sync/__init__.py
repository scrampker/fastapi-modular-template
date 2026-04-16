"""scottycore.services.sync — mesh synchronisation subsystem.

Public surface:
  models   — SQLAlchemy ORM models (PlatformNode, SyncPeer, …)
  schemas  — Pydantic schemas (SyncMode, NodeInfo, PullRequest, …)
  service  — SyncService (CRUD + auth helpers)
  engine   — MeshSyncEngine (lifecycle, per-peer async tasks)
  auth     — peer key generation and verification
  transport — httpx calls to peer /sync/* endpoints
  repository — raw DB helpers (low-level; prefer using SyncService)
"""

from scottycore.services.sync.engine import MeshSyncEngine
from scottycore.services.sync.models import (
    PeerApiKey,
    PlatformNode,
    SyncConflict,
    SyncPeer,
    SyncRowOrigin,
    SyncTenantSubscription,
)
from scottycore.services.sync.schemas import (
    NodeInfo,
    PeerCreateResponse,
    PeerSyncStatus,
    PullRequest,
    PushRequest,
    PushResponse,
    SyncConflictRead,
    SyncConflictResolution,
    SyncDirection,
    SyncMode,
    SyncPeerCreate,
    SyncPeerRead,
    SyncPeerUpdate,
    SyncStatus,
    SyncTenantSubscriptionCreate,
    SyncTenantSubscriptionRead,
)
from scottycore.services.sync.service import SyncService

__all__ = [
    # Engine
    "MeshSyncEngine",
    # Models
    "PeerApiKey",
    "PlatformNode",
    "SyncConflict",
    "SyncPeer",
    "SyncRowOrigin",
    "SyncTenantSubscription",
    # Schemas
    "NodeInfo",
    "PeerCreateResponse",
    "PeerSyncStatus",
    "PullRequest",
    "PushRequest",
    "PushResponse",
    "SyncConflictRead",
    "SyncConflictResolution",
    "SyncDirection",
    "SyncMode",
    "SyncPeerCreate",
    "SyncPeerRead",
    "SyncPeerUpdate",
    "SyncStatus",
    "SyncTenantSubscriptionCreate",
    "SyncTenantSubscriptionRead",
    # Service
    "SyncService",
]
