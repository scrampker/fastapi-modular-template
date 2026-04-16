"""MeshSyncEngine — lifecycle object that drives async pull-then-push cycles.

One asyncio.Task is maintained per enabled peer.  Each task runs a pull-then-
push cycle on a configurable interval (default 30 s) with exponential backoff
on error.

Echo prevention: every pull request passes ``exclude_origins=[local_node_id]``
so peers do not re-send rows that originated here.

Conflict resolution: LWW on ``updated_at``; ties broken by ``origin_node_id``
lexicographic order.

Clock skew assumption: ``since`` is set to ``peer.last_pulled_ts - CLOCK_SKEW_TOLERANCE_S``
so that rows updated within the tolerance window are re-fetched.  Rows must be
upsert-idempotent (session.merge() on primary key) — the engine enforces this
through the BackupService restore path.

This class intentionally contains NO HTTP server logic — it is a pure
asyncio object that the lifespan starts/stops.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import tarfile
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scottycore.services.backup.service import BackupService
from scottycore.services.sync import repository as repo
from scottycore.services.sync.models import PlatformNode
from scottycore.services.sync.service import SyncService
from scottycore.services.sync.transport import SyncTransportError, call_peer_pull, call_peer_push

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_S = 30
_MAX_BACKOFF_S = 3600
_BACKOFF_MULTIPLIER = 2
# Rows updated within this many seconds before last_pulled_ts are re-fetched
# to compensate for clock skew between mesh nodes.
CLOCK_SKEW_TOLERANCE_S = 60
# Maximum number of concurrent peer pull tasks; prevents unbounded fan-out.
_DEFAULT_MAX_CONCURRENT = 8


class MeshSyncEngine:
    """Drives the pull-then-push cycle for all enabled peers.

    Instantiated once in the app lifespan; not part of ServiceRegistry.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        sync_service: SyncService,
        backup_service: BackupService,
        local_node: PlatformNode,
        interval_seconds: int = _DEFAULT_INTERVAL_S,
        max_concurrent: int = _DEFAULT_MAX_CONCURRENT,
    ) -> None:
        self._sf = session_factory
        self._sync = sync_service
        self._backup = backup_service
        self._local_node = local_node
        self._interval = interval_seconds
        self._semaphore = asyncio.Semaphore(max_concurrent)
        # peer_id → running asyncio.Task
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._running = False
        # Per-peer kick events (set by kick()) to skip the sleep delay.
        self._kick_events: dict[str, asyncio.Event] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start one task per currently-enabled peer."""
        self._running = True
        async with self._sf() as session:
            peers = await repo.list_enabled_peers(session)
        for peer in peers:
            self._start_peer_task(peer.id)
        logger.info(
            "MeshSyncEngine started — %d peer tasks launched", len(self._tasks)
        )

    async def stop(self) -> None:
        """Cancel all running tasks and wait for them to finish."""
        self._running = False
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        logger.info("MeshSyncEngine stopped")

    def _start_peer_task(self, peer_id: str) -> None:
        if peer_id in self._tasks and not self._tasks[peer_id].done():
            return
        event = asyncio.Event()
        self._kick_events[peer_id] = event
        task = asyncio.create_task(
            self._peer_loop(peer_id, event), name=f"sync-peer-{peer_id}"
        )
        self._tasks[peer_id] = task
        logger.debug("Sync task started for peer %s", peer_id)

    def _stop_peer_task(self, peer_id: str) -> None:
        task = self._tasks.pop(peer_id, None)
        if task and not task.done():
            task.cancel()
        self._kick_events.pop(peer_id, None)
        logger.debug("Sync task stopped for peer %s", peer_id)

    # ── Per-peer loop ─────────────────────────────────────────────────────

    async def _peer_loop(self, peer_id: str, kick_event: asyncio.Event) -> None:
        """Pull-then-push cycle for a single peer.

        On success: reset backoff, sleep interval.
        On error: exponential backoff up to _MAX_BACKOFF_S.

        The semaphore caps concurrent active pulls at max_concurrent to
        prevent unbounded task fan-out on large meshes.
        """
        while self._running:
            try:
                async with self._semaphore:
                    await self._sync_cycle(peer_id)
                sleep_s = self._interval
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("Sync cycle error for peer %s: %s", peer_id, exc)
                sleep_s = await self._handle_error(peer_id, exc)

            # Sleep or wake early via kick().
            try:
                await asyncio.wait_for(kick_event.wait(), timeout=float(sleep_s))
                kick_event.clear()
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                return

    async def _handle_error(self, peer_id: str, exc: Exception) -> int:
        """Record error, compute and return next backoff in seconds."""
        async with self._sf() as session:
            peer = await repo.get_peer(session, peer_id)
            if peer is None:
                return _MAX_BACKOFF_S
            current_backoff = peer.backoff_seconds or 0
            new_backoff = min(
                max(current_backoff * _BACKOFF_MULTIPLIER, _DEFAULT_INTERVAL_S),
                _MAX_BACKOFF_S,
            )
            await repo.record_peer_error(session, peer_id, str(exc), new_backoff)
            await session.commit()
        return new_backoff

    # ── Sync cycle ────────────────────────────────────────────────────────

    async def _sync_cycle(self, peer_id: str) -> None:
        """Execute one pull-then-push exchange with a peer."""
        async with self._sf() as session:
            peer = await repo.get_peer(session, peer_id)

        if peer is None or not peer.enabled:
            logger.debug("Peer %s disabled or missing — skipping cycle", peer_id)
            return

        if not peer.their_key_for_us:
            logger.debug("Peer %s has no their_key_for_us — skipping pull", peer_id)
            # Can still push if we have our_key issued.
            await self._push_cycle(peer, b"")
            return

        # Decrypt the stored peer key (vault-encoded or plaintext fallback).
        from scottycore.services.sync.service import decrypt_their_key
        decrypted_key = decrypt_their_key(peer.their_key_for_us)
        if not decrypted_key:
            logger.warning("Peer %s key could not be decrypted — skipping cycle", peer_id)
            return

        # Determine scope for this peer.
        scope = await self._build_scope(peer)

        # Apply clock-skew tolerance: subtract CLOCK_SKEW_TOLERANCE_S from
        # last_pulled_ts so rows updated near the boundary are re-fetched.
        pull_since: datetime | None = None
        if peer.last_pulled_ts is not None:
            pull_since = peer.last_pulled_ts - timedelta(seconds=CLOCK_SKEW_TOLERANCE_S)

        # ── Pull ────────────────────────────────────────────────────
        bundle_bytes = await call_peer_pull(
            base_url=peer.base_url,
            their_key_for_us=decrypted_key,
            since=pull_since,
            scope=scope,
            exclude_origins=[self._local_node.id],
        )

        rows_applied, rows_skipped, conflicts = await self._apply_bundle(
            bundle_bytes, peer_id
        )
        now = datetime.now(timezone.utc)
        async with self._sf() as session:
            await repo.record_pull_success(session, peer_id, now)
            await session.commit()

        logger.info(
            "Peer %s pull complete — applied=%d skipped=%d conflicts=%d",
            peer_id, rows_applied, rows_skipped, conflicts,
        )

        # ── Push ────────────────────────────────────────────────────
        # Re-fetch peer to get the latest last_pushed_ts for the delta.
        async with self._sf() as session:
            peer = await repo.get_peer(session, peer_id)

        if peer is not None:
            push_bundle = await self._build_push_bundle(peer)
            await self._push_cycle(peer, push_bundle)

    async def _push_cycle(self, peer: Any, bundle_bytes: bytes) -> None:
        """Push our local delta to the peer."""
        if not peer.their_key_for_us:
            logger.debug("Peer %s has no their_key_for_us — skipping push", peer.id)
            return

        if not bundle_bytes:
            logger.debug("No data to push to peer %s", peer.id)
            return

        # Decrypt stored key before use.
        from scottycore.services.sync.service import decrypt_their_key
        decrypted_key = decrypt_their_key(peer.their_key_for_us)
        if not decrypted_key:
            logger.warning("Peer %s key could not be decrypted — skipping push", peer.id)
            return

        push_resp = await call_peer_push(
            base_url=peer.base_url,
            their_key_for_us=decrypted_key,
            bundle_bytes=bundle_bytes,
            our_node_id=self._local_node.id,
        )
        now = datetime.now(timezone.utc)
        async with self._sf() as session:
            await repo.record_push_success(session, peer.id, now)
            await session.commit()
        logger.info(
            "Peer %s push complete — applied=%d skipped=%d conflicts=%d",
            peer.id,
            push_resp.rows_applied,
            push_resp.rows_skipped,
            push_resp.conflicts_created,
        )

    async def _build_scope(self, peer: Any) -> dict[str, Any]:
        """Derive a scope dict for the pull request based on peer's sync_mode."""
        if peer.sync_mode == "full":
            return {}
        if peer.sync_mode == "tenants_only":
            return {"mode": "tenants_only"}
        # "selected" — pull subscribed tenant IDs.
        async with self._sf() as session:
            subs = await repo.list_tenant_subs(session, peer.id)
        tenant_ids = [
            s.tenant_id for s in subs if s.direction in ("in", "both")
        ]
        return {"mode": "selected", "tenant_ids": tenant_ids}

    async def _build_push_bundle(self, peer: Any) -> bytes:
        """Build a delta export bundle to push to the peer.

        Uses BackupService.export_platform_delta() so only rows changed since
        last_pushed_ts (minus clock-skew tolerance) are included.
        """
        try:
            # Apply the same clock-skew tolerance to the push delta.
            push_since: datetime | None = None
            if peer.last_pushed_ts is not None:
                push_since = peer.last_pushed_ts - timedelta(seconds=CLOCK_SKEW_TOLERANCE_S)

            bundle = await self._backup.export_platform_delta(
                since=push_since,
                exclude_origins=[self._local_node.id],
                user_id=__import__("uuid").UUID("00000000-0000-0000-0000-000000000000"),
                ip="sync-engine",
            )
            return bundle
        except Exception as exc:
            logger.warning("Failed to build push bundle: %s", exc)
            return b""

    # ── Inbound bundle application (called by /sync/push handler) ─────────

    async def apply_inbound_bundle(
        self,
        bundle_bytes: bytes,
        origin_node_id: str,
        peer_id: str,
    ) -> tuple[int, int, int]:
        """Apply a bundle received from a peer.

        Returns ``(rows_applied, rows_skipped, conflicts_created)``.
        Called from the /sync/push HTTP handler.
        """
        return await self._apply_bundle(bundle_bytes, peer_id, origin_node_id)

    async def _apply_bundle(
        self,
        bundle_bytes: bytes,
        peer_id: str,
        origin_node_id: str | None = None,
    ) -> tuple[int, int, int]:
        """Deserialise a tarball and apply rows with LWW conflict resolution."""
        if not bundle_bytes:
            return 0, 0, 0

        try:
            manifest, contributor_data = _extract_bundle(bundle_bytes)
        except Exception as exc:
            logger.warning("Failed to parse inbound bundle from peer %s: %s", peer_id, exc)
            return 0, 0, 0

        rows_applied = 0
        rows_skipped = 0
        conflicts_created = 0

        for contributor_id, data in contributor_data.items():
            contributor = self._backup._contributors.get(contributor_id)  # noqa: SLF001
            if contributor is None:
                logger.debug("No contributor %s registered — skipping", contributor_id)
                continue

            rows = data.get("rows", [])

            # exclude_origins post-filter: drop rows that originated here.
            # Performance tradeoff: in-Python loop vs SQL JOIN on sync_row_origin.
            # We intentionally keep this outside SQL to avoid coupling contributors
            # to the sync schema and to work before sync tables exist (cold start).
            exclude = [self._local_node.id]
            filtered_rows = await self._filter_rows_by_origin(rows, contributor_id, exclude)

            applied, skipped, conflicts = await self._apply_contributor_rows(
                contributor=contributor,
                contributor_id=contributor_id,
                rows=filtered_rows,
                peer_id=peer_id,
                origin_node_id=origin_node_id or "unknown",
            )
            rows_applied += applied
            rows_skipped += skipped + (len(rows) - len(filtered_rows))
            conflicts_created += conflicts

        return rows_applied, rows_skipped, conflicts_created

    async def _filter_rows_by_origin(
        self,
        rows: list[dict[str, Any]],
        contributor_id: str,
        exclude_origins: list[str],
    ) -> list[dict[str, Any]]:
        """Drop rows whose recorded origin_node_id is in exclude_origins.

        Looks up each row's origin in SyncRowOrigin.  Rows with no origin
        record pass through — they are newly seen rows.
        """
        if not exclude_origins or not rows:
            return rows

        kept: list[dict[str, Any]] = []
        async with self._sf() as session:
            for row in rows:
                row_id = str(row.get("id", ""))
                if not row_id:
                    kept.append(row)
                    continue
                origin = await repo.get_row_origin(session, contributor_id, row_id)
                if origin is None or origin.origin_node_id not in exclude_origins:
                    kept.append(row)
        return kept

    async def _apply_contributor_rows(
        self,
        contributor: Any,
        contributor_id: str,
        rows: list[dict[str, Any]],
        peer_id: str,
        origin_node_id: str,
    ) -> tuple[int, int, int]:
        """Apply rows for a single contributor with true LWW conflict resolution.

        For each row:
          1. Call contributor.get_row() to fetch the local version (if available).
          2. Compare ``updated_at``; ties broken by origin_node_id lex order.
          3. If remote wins (or no local version): upsert + record origin.
          4. If local wins: record SyncConflict row, keep local.
        """
        applied = 0
        skipped = 0
        conflicts = 0

        from scottycore.services.backup.schemas import BackupScope

        for row in rows:
            try:
                remote_updated_at = _parse_ts(row.get("updated_at"))
                row_id = str(row.get("id", ""))

                # True LWW: read the local row's updated_at via get_row() if contributor supports it.
                local_row: dict[str, Any] | None = None
                if row_id and hasattr(contributor, "get_row"):
                    local_row = await contributor.get_row(contributor_id, row_id)

                local_updated_at = _parse_ts(local_row.get("updated_at")) if local_row else None

                # Determine winner.
                remote_wins = _lww_remote_wins(
                    remote_updated_at, local_updated_at, origin_node_id, self._local_node.id
                )

                if not remote_wins and local_updated_at is not None and remote_updated_at is not None:
                    # Local wins — record conflict without applying remote.
                    async with self._sf() as session:
                        import json as _json
                        await repo.create_conflict(
                            session,
                            peer_id=peer_id,
                            tenant_id=row.get("tenant_id"),
                            resource_type=contributor_id,
                            resource_id=row_id,
                            local_payload=_json.dumps(local_row, default=str),
                            remote_payload=_json.dumps(row, default=str),
                            resolution="lww_local_kept",
                        )
                        await session.commit()
                    conflicts += 1
                    continue

                # Remote wins (or no local row to compare): apply.
                await contributor.restore(
                    scope=BackupScope.PLATFORM,
                    tenant_id=row.get("tenant_id"),
                    rows=[row],
                    files=[],
                    session_factory=self._sf,
                )
                await self._sync.record_row_origin(
                    contributor_id, row_id, origin_node_id
                )
                applied += 1

            except Exception as exc:
                logger.warning(
                    "Failed to apply row from contributor %s peer %s: %s",
                    contributor_id, peer_id, exc,
                )
                skipped += 1

        return applied, skipped, conflicts

    # ── Task management ───────────────────────────────────────────────────

    def kick(self, peer_id: str) -> None:
        """Trigger an immediate sync cycle for a peer (skips sleep)."""
        event = self._kick_events.get(peer_id)
        if event is not None:
            event.set()
        else:
            logger.warning("kick() called for unknown/stopped peer %s", peer_id)

    async def on_peer_config_change(self) -> None:
        """Reconcile running tasks with current enabled-peer set.

        Call this after creating, enabling, or disabling a peer so the engine
        starts or stops tasks accordingly.
        """
        async with self._sf() as session:
            enabled = {p.id for p in await repo.list_enabled_peers(session)}

        running = set(self._tasks.keys())

        for peer_id in enabled - running:
            self._start_peer_task(peer_id)

        for peer_id in running - enabled:
            self._stop_peer_task(peer_id)

    @property
    def running_peer_ids(self) -> set[str]:
        return {pid for pid, t in self._tasks.items() if not t.done()}


# ── LWW helper ──────────────────────────────────────────────────────────────


def _lww_remote_wins(
    remote_ts: datetime | None,
    local_ts: datetime | None,
    remote_node_id: str,
    local_node_id: str,
) -> bool:
    """Return True if the remote row should be applied.

    Rules:
    - If no remote timestamp, apply (no basis to reject).
    - If no local timestamp, apply (row is new locally).
    - If remote > local, apply.
    - If remote < local, keep local (return False).
    - If equal, break tie by node_id lex: higher string wins.
    """
    if remote_ts is None or local_ts is None:
        return True
    if remote_ts > local_ts:
        return True
    if remote_ts < local_ts:
        return False
    # Equal timestamps — lex tie-break; higher node_id wins.
    return remote_node_id >= local_node_id


# ── Bundle parsing (mirrors BackupService._extract_bundle) ─────────────────


def _extract_bundle(
    bundle_bytes: bytes,
) -> tuple[Any, dict[str, dict[str, Any]]]:
    """Extract manifest and per-contributor row data from a .tar.gz bundle."""
    from scottycore.services.backup.schemas import BackupManifest

    buf = io.BytesIO(bundle_bytes)
    contributor_data: dict[str, dict[str, Any]] = {}

    with tarfile.open(fileobj=buf, mode="r:gz") as tf:
        manifest_member = tf.getmember("manifest.json")
        manifest_fobj = tf.extractfile(manifest_member)
        if manifest_fobj is None:
            raise ValueError("Missing manifest.json in bundle")
        manifest = BackupManifest.model_validate_json(manifest_fobj.read())

        for member in tf.getmembers():
            name = member.name
            if name.startswith("data/") and name.endswith(".json"):
                cid = name[len("data/"):-len(".json")]
                fobj = tf.extractfile(member)
                if fobj is not None:
                    contributor_data.setdefault(cid, {"rows": [], "files": []})
                    contributor_data[cid]["rows"] = json.loads(fobj.read())

    return manifest, contributor_data


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
