"""BackupScheduler — asyncio loop that drives recurring backups.

Lifecycle
---------
* :meth:`start` launches the background task and returns immediately.
* :meth:`stop` cancels it and awaits shutdown.
* The task wakes every ``tick_seconds`` (default 30s). On each tick it queries
  all active schedules whose ``next_run_at`` is in the past or null, runs them
  one at a time, then sleeps until the next tick.

Concurrency
-----------
The scheduler does *not* run jobs in parallel. That's deliberate — backup I/O
is heavy and overlapping exports can thrash the sink. If a run is in flight
when the next tick arrives, the loop just skips and retries on the tick after.

Run dispatch
------------
Each schedule is materialised into a :class:`BackupRun` row in the
``pending`` state before the export starts, then transitioned to ``running``
→ ``success``/``failed``. Failed runs leave ``error`` populated and do NOT
advance ``next_run_at`` — the scheduler will retry on the next tick. Consumers
who want retry backoff should wrap the scheduler themselves.

Retention
---------
After a successful run, older snapshots for the same (schedule_id, scope,
tenant_slug) are pruned according to ``retention_days`` and/or ``keep_last``.
Both settings apply together (logical AND): a snapshot is kept if it would
be kept by either rule.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from croniter import croniter  # type: ignore[import-untyped]
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scottycore.services.backup.crypto import encrypt_bundle, fingerprint
from scottycore.services.backup.models import (
    KIND_DELTA,
    KIND_FULL,
    SCOPE_PLATFORM,
    SCOPE_TENANT,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    BackupRun,
    BackupSchedule,
)
from scottycore.services.backup.schemas import BackupScope
from scottycore.services.backup.service import BackupService
from scottycore.services.backup.sinks import BackupBlob, LocalDiskSink, ScottyDevSink, StorageSink

_log = logging.getLogger(__name__)

# UUID used as user_id on scheduler-initiated runs so audit rows are grouped.
_SCHEDULER_USER_ID = UUID("00000000-0000-0000-0000-00000000bcac")


class BackupScheduler:
    """Async cron driver for :class:`BackupSchedule` rows."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        backup_service: BackupService,
        passphrase_provider: "PassphraseProvider | None" = None,
        tick_seconds: float = 30.0,
    ) -> None:
        self._factory = session_factory
        self._svc = backup_service
        self._pp = passphrase_provider or _NullPassphraseProvider()
        self._tick = tick_seconds
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="backup-scheduler")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None

    # ── Core loop ─────────────────────────────────────────────────────────

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.tick_once()
            except Exception as exc:  # noqa: BLE001
                _log.exception("scheduler tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._tick)
            except asyncio.TimeoutError:
                pass

    async def tick_once(self) -> list[str]:
        """Run all schedules whose ``next_run_at`` has passed. Returns run IDs."""
        due = await self._due_schedules()
        run_ids: list[str] = []
        for schedule in due:
            run_id = await self._dispatch(schedule)
            if run_id:
                run_ids.append(run_id)
        return run_ids

    async def _due_schedules(self) -> list[BackupSchedule]:
        now = datetime.now(timezone.utc)
        async with self._factory() as s:
            q = select(BackupSchedule).where(BackupSchedule.is_active.is_(True))
            rows = list((await s.scalars(q)).all())
        out: list[BackupSchedule] = []
        for r in rows:
            if r.next_run_at is None:
                out.append(r)
                continue
            nra = r.next_run_at
            if nra.tzinfo is None:
                nra = nra.replace(tzinfo=timezone.utc)
            if nra <= now:
                out.append(r)
        return out

    # ── Dispatch ──────────────────────────────────────────────────────────

    async def _dispatch(self, schedule: BackupSchedule) -> str | None:
        started_at = datetime.now(timezone.utc)

        # 1. Pre-create the run row so we have an audit trail even if export dies.
        run_id = await self._mark_run_pending(schedule, started_at)

        try:
            await self._mark_run_status(run_id, STATUS_RUNNING)

            bundle_bytes, tenant_slug = await self._run_export(schedule)

            encrypted, key_fp = False, None
            passphrase = await self._pp.resolve(schedule)
            if passphrase:
                bundle_bytes = await encrypt_bundle(bundle_bytes, passphrase)
                encrypted = True
                key_fp = fingerprint(passphrase)

            blob = BackupBlob(
                data=bundle_bytes,
                sha256=hashlib.sha256(bundle_bytes).hexdigest(),
                size=len(bundle_bytes),
                app_slug=self._svc._app_name,  # noqa: SLF001
                scope=schedule.scope,
                kind=schedule.kind,
                created_at=started_at,
                encrypted=encrypted,
                key_fingerprint=key_fp,
                tenant_slug=tenant_slug,
            )

            sink = _build_sink_from_config(schedule.sink_type, schedule.sink_config)
            result = await sink.put(blob)

            await self._mark_run_success(
                run_id=run_id,
                blob=blob,
                sink_locator=result.locator,
                bytes_written=result.bytes_written,
                finished_at=datetime.now(timezone.utc),
            )

            await self._advance_next_run_at(schedule, started_at)

            if schedule.retention_days or schedule.keep_last:
                await self._prune(schedule, sink)

            return run_id
        except Exception as exc:  # noqa: BLE001
            _log.exception("schedule %s failed", schedule.id)
            await self._mark_run_failed(run_id, str(exc))
            return run_id

    # ── Export drivers ────────────────────────────────────────────────────

    async def _run_export(self, schedule: BackupSchedule) -> tuple[bytes, str | None]:
        if schedule.scope == SCOPE_PLATFORM:
            if schedule.kind == KIND_DELTA:
                bundle = await self._svc.export_platform_delta(
                    since=_last_success_time(schedule),
                    exclude_origins=None,
                    user_id=_SCHEDULER_USER_ID,
                    ip="scheduler",
                )
            else:
                bundle = await self._svc.export_platform(
                    user_id=_SCHEDULER_USER_ID, ip="scheduler"
                )
            return bundle, None

        if schedule.scope == SCOPE_TENANT:
            if not schedule.tenant_id or not schedule.tenant_slug:
                raise ValueError("tenant schedule missing tenant_id/slug")
            if schedule.kind == KIND_DELTA:
                bundle = await self._svc.export_tenant_delta(
                    tenant_id=schedule.tenant_id,
                    tenant_slug=schedule.tenant_slug,
                    since=None,
                    exclude_origins=None,
                    user_id=_SCHEDULER_USER_ID,
                    ip="scheduler",
                )
            else:
                bundle = await self._svc.export_tenant(
                    tenant_id=schedule.tenant_id,
                    tenant_slug=schedule.tenant_slug,
                    user_id=_SCHEDULER_USER_ID,
                    ip="scheduler",
                )
            return bundle, schedule.tenant_slug

        raise ValueError(f"unknown scope: {schedule.scope}")

    # ── Run state helpers ─────────────────────────────────────────────────

    async def _mark_run_pending(
        self, schedule: BackupSchedule, started_at: datetime
    ) -> str:
        async with self._factory() as s:
            run = BackupRun(
                schedule_id=schedule.id,
                app_slug=self._svc._app_name,  # noqa: SLF001
                scope=schedule.scope,
                tenant_slug=schedule.tenant_slug,
                kind=schedule.kind,
                status=STATUS_PENDING,
                sink_type=schedule.sink_type,
                started_at=started_at,
                managed_by=schedule.managed_by,
            )
            s.add(run)
            await s.commit()
            await s.refresh(run)
            return run.id

    async def _mark_run_status(self, run_id: str, status: str) -> None:
        async with self._factory() as s:
            run = await s.get(BackupRun, run_id)
            if run is not None:
                run.status = status
                await s.commit()

    async def _mark_run_success(
        self,
        *,
        run_id: str,
        blob: BackupBlob,
        sink_locator: str,
        bytes_written: int,
        finished_at: datetime,
    ) -> None:
        async with self._factory() as s:
            run = await s.get(BackupRun, run_id)
            if run is None:
                return
            run.status = STATUS_SUCCESS
            run.sink_locator = sink_locator
            run.bytes_written = bytes_written
            run.sha256 = blob.sha256
            run.encrypted = blob.encrypted
            run.key_fingerprint = blob.key_fingerprint
            run.finished_at = finished_at
            await s.commit()

    async def _mark_run_failed(self, run_id: str, err: str) -> None:
        async with self._factory() as s:
            run = await s.get(BackupRun, run_id)
            if run is None:
                return
            run.status = STATUS_FAILED
            run.error = err
            run.finished_at = datetime.now(timezone.utc)
            await s.commit()

    async def _advance_next_run_at(
        self, schedule: BackupSchedule, base: datetime
    ) -> None:
        if not schedule.cron_expr:
            async with self._factory() as s:
                row = await s.get(BackupSchedule, schedule.id)
                if row is not None:
                    row.next_run_at = None
                    row.is_active = False  # one-shot schedule
                    await s.commit()
            return

        nxt = croniter(schedule.cron_expr, base).get_next(datetime)
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=timezone.utc)
        async with self._factory() as s:
            row = await s.get(BackupSchedule, schedule.id)
            if row is not None:
                row.next_run_at = nxt
                await s.commit()

    # ── Retention ─────────────────────────────────────────────────────────

    async def _prune(self, schedule: BackupSchedule, sink: StorageSink) -> None:
        keep_cutoff: datetime | None = None
        if schedule.retention_days:
            keep_cutoff = datetime.now(timezone.utc) - timedelta(
                days=schedule.retention_days
            )

        async with self._factory() as s:
            q = (
                select(BackupRun)
                .where(
                    and_(
                        BackupRun.schedule_id == schedule.id,
                        BackupRun.status == STATUS_SUCCESS,
                    )
                )
                .order_by(BackupRun.created_at.desc())
            )
            runs = list((await s.scalars(q)).all())

        # Compute the "keep" set.
        keep: set[str] = set()
        if schedule.keep_last:
            for r in runs[: schedule.keep_last]:
                keep.add(r.id)
        if keep_cutoff is not None:
            for r in runs:
                ts = r.created_at
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= keep_cutoff:
                    keep.add(r.id)
        elif schedule.keep_last is None:
            # No retention config means keep everything.
            return

        to_delete = [r for r in runs if r.id not in keep]
        for r in to_delete:
            if r.sink_locator:
                try:
                    await sink.delete(r.sink_locator)
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "prune: failed to delete %s: %s", r.sink_locator, exc
                    )
            async with self._factory() as s:
                obj = await s.get(BackupRun, r.id)
                if obj is not None:
                    await s.delete(obj)
                    await s.commit()


# ── Passphrase plumbing ────────────────────────────────────────────────────


class PassphraseProvider:
    """Abstract — resolve the correct passphrase for a given schedule.

    Production implementations pull from Vault, a local keyring, or ScottyDev.
    Tests typically use :class:`StaticPassphraseProvider`.
    """

    async def resolve(self, schedule: BackupSchedule) -> str | None:
        raise NotImplementedError


class StaticPassphraseProvider(PassphraseProvider):
    def __init__(self, passphrase: str | None) -> None:
        self._pp = passphrase

    async def resolve(self, schedule: BackupSchedule) -> str | None:
        return self._pp


class _NullPassphraseProvider(PassphraseProvider):
    async def resolve(self, schedule: BackupSchedule) -> str | None:
        return None


# ── Helpers ────────────────────────────────────────────────────────────────


def _build_sink_from_config(sink_type: str, config: dict[str, Any]) -> StorageSink:
    if sink_type == "local_disk":
        return LocalDiskSink(config.get("root_dir") or "/app/data/backups")
    if sink_type == "scottydev":
        base = config.get("base_url")
        if not base:
            raise ValueError("orchestrator sink requires base_url")
        return ScottyDevSink(base_url=base, token=config.get("token"))
    if sink_type == "git_repo":
        from scottycore.services.backup.sinks import GitRepoSink

        repo_url = config.get("repo_url")
        if not repo_url:
            raise ValueError("git_repo sink requires repo_url")
        clone_dir = config.get("clone_dir") or "/app/data/backups-git"
        return GitRepoSink(
            repo_url=repo_url,
            local_clone_dir=clone_dir,
            branch=config.get("branch") or "backups",
            path_template=(
                config.get("path_template")
                or "snapshots/{app_slug}/{scope}/{tenant_slug}"
            ),
            lfs_enabled=bool(config.get("lfs_enabled", True)),
        )
    raise ValueError(f"unsupported sink_type for scheduler: {sink_type}")


def _last_success_time(schedule: BackupSchedule) -> datetime | None:
    """Best-effort 'since' anchor for delta exports. None means full fallback."""
    # For simplicity, the scheduler doesn't look up the previous run here —
    # BackupService.export_platform_delta accepts None to mean "full export".
    # A future pass can query BackupRun for the latest success and hand it
    # down; the engine's delta mechanism is already prepared for that.
    return None
