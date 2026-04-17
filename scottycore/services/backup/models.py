"""SQLAlchemy models for backup schedules and run history.

These tables live in scottycore so the same schema works locally in a
consumer app *and* centrally in ScottyDev. The ``managed_by`` column is what
differentiates the two views of the same logical schedule:

* ``managed_by='local'`` — the schedule is owned by this app. It runs here,
  and nothing upstream will change it.
* ``managed_by='scottydev'`` — the schedule is owned by ScottyDev and was
  pushed down during enrollment or by the sync loop. Local UI shows it
  read-only with a [🌐 ScottyDev] badge and "Promote to local" / "Detach"
  actions. ``remote_id`` holds ScottyDev's UUID for the canonical row.

Passphrase model
----------------
* :attr:`App.backup_passphrase_fingerprint` (recorded out-of-band in the app
  domain model — not on this table) pins the "current" passphrase for the
  whole app.
* :attr:`BackupSchedule.passphrase_override_fingerprint` overrides that on a
  per-schedule basis. Off by default; advanced users only.
* :attr:`BackupRun.key_fingerprint` records which passphrase the snapshot was
  actually encrypted with, so restores can pick the right saved key.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from scottycore.core.database import Base, TimestampMixin, new_uuid

#: ``managed_by`` values.
MANAGED_LOCAL = "local"
MANAGED_SCOTTYDEV = "scottydev"

#: ``scope`` values for both schedules and runs.
SCOPE_PLATFORM = "platform"
SCOPE_TENANT = "tenant"

#: ``kind`` values.
KIND_FULL = "full"
KIND_DELTA = "delta"

#: ``status`` values on BackupRun.
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"


class BackupSchedule(Base, TimestampMixin):
    """A recurring or one-off backup rule.

    Execution is owned by the asyncio scheduling engine (see
    :mod:`scottycore.services.backup.scheduler`). That engine wakes up, finds
    schedules whose ``next_run_at`` has passed, and records a ``BackupRun``.
    """

    __tablename__ = "backup_schedules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)  # platform | tenant
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    tenant_slug: Mapped[str | None] = mapped_column(String(200), nullable=True)

    #: which sink to write to (one of sink_type values registered in
    #: :mod:`scottycore.services.backup.sinks`). The scheduler resolves the
    #: concrete sink instance from config.
    sink_type: Mapped[str] = mapped_column(String(32), nullable=False)
    #: free-form JSON config for the sink (root dir, host+user, base URL, …).
    sink_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    #: cron expression; None means "one-shot, run-now" (``next_run_at`` carries
    #: the actual time).
    cron_expr: Mapped[str | None] = mapped_column(String(200), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    kind: Mapped[str] = mapped_column(String(16), nullable=False, default=KIND_FULL)
    retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    keep_last: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Advanced: override the app-level passphrase for this schedule. If set,
    #: the scheduler will prompt / look up a schedule-specific passphrase and
    #: stamp its fingerprint here. Optional.
    passphrase_override_fingerprint: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # ── Dual management ───────────────────────────────────────────────────

    #: ``"local"`` or ``"scottydev"``. See module docstring.
    managed_by: Mapped[str] = mapped_column(
        String(16), nullable=False, default=MANAGED_LOCAL
    )
    #: ScottyDev's UUID for this schedule when ``managed_by='scottydev'``.
    remote_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    #: Last time the sync loop reconciled this row with the upstream copy.
    last_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    __table_args__ = (
        UniqueConstraint("remote_id", name="uq_backup_schedules_remote_id"),
    )


class BackupRun(Base, TimestampMixin):
    """Outcome of a single backup attempt (scheduled or ad-hoc)."""

    __tablename__ = "backup_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    schedule_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    app_slug: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    tenant_slug: Mapped[str | None] = mapped_column(String(200), nullable=True)

    kind: Mapped[str] = mapped_column(String(16), nullable=False, default=KIND_FULL)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_PENDING)

    sink_type: Mapped[str] = mapped_column(String(32), nullable=False)
    sink_locator: Mapped[str | None] = mapped_column(Text, nullable=True)
    bytes_written: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    encrypted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: SHA-256[:4] hex of the passphrase used. 8 chars. Recorded on every
    #: encrypted run so users can match the snapshot to a saved key.
    key_fingerprint: Mapped[str | None] = mapped_column(String(16), nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Origin of the run — "local" or "scottydev". Matches :class:`BackupSchedule`
    #: but lives here too so ad-hoc runs with no schedule can be attributed.
    managed_by: Mapped[str] = mapped_column(
        String(16), nullable=False, default=MANAGED_LOCAL
    )
