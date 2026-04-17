"""Backup schedules + runs tables.

Revision ID: 0003_backup_schedules_and_runs
Revises: 0002_hipaa_security_patterns
Create Date: 2026-04-16

Adds:
  - backup_schedules: recurring/one-off backup rules with dual-management
    (``managed_by`` = "local" | "scottydev") and optional per-schedule
    passphrase override fingerprint.
  - backup_runs: per-attempt outcome log with sink locator, checksum and
    key fingerprint.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_backup_schedules_and_runs"
down_revision: str | None = "0002_hipaa_security_patterns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "backup_schedules",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("tenant_id", sa.String(36), nullable=True),
        sa.Column("tenant_slug", sa.String(200), nullable=True),
        sa.Column("sink_type", sa.String(32), nullable=False),
        sa.Column("sink_config", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("cron_expr", sa.String(200), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False, server_default="full"),
        sa.Column("retention_days", sa.Integer(), nullable=True),
        sa.Column("keep_last", sa.Integer(), nullable=True),
        sa.Column("passphrase_override_fingerprint", sa.String(16), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("managed_by", sa.String(16), nullable=False, server_default="local"),
        sa.Column("remote_id", sa.String(36), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(36), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("remote_id", name="uq_backup_schedules_remote_id"),
    )
    op.create_index(
        "ix_backup_schedules_tenant_id", "backup_schedules", ["tenant_id"]
    )
    op.create_index(
        "ix_backup_schedules_next_run_at", "backup_schedules", ["next_run_at"]
    )
    op.create_index(
        "ix_backup_schedules_remote_id", "backup_schedules", ["remote_id"]
    )

    op.create_table(
        "backup_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("schedule_id", sa.String(36), nullable=True),
        sa.Column("app_slug", sa.String(200), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("tenant_slug", sa.String(200), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False, server_default="full"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("sink_type", sa.String(32), nullable=False),
        sa.Column("sink_locator", sa.Text(), nullable=True),
        sa.Column("bytes_written", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("encrypted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("key_fingerprint", sa.String(16), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("managed_by", sa.String(16), nullable=False, server_default="local"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_backup_runs_schedule_id", "backup_runs", ["schedule_id"])
    op.create_index("ix_backup_runs_app_slug", "backup_runs", ["app_slug"])


def downgrade() -> None:
    op.drop_index("ix_backup_runs_app_slug", table_name="backup_runs")
    op.drop_index("ix_backup_runs_schedule_id", table_name="backup_runs")
    op.drop_table("backup_runs")

    op.drop_index("ix_backup_schedules_remote_id", table_name="backup_schedules")
    op.drop_index("ix_backup_schedules_next_run_at", table_name="backup_schedules")
    op.drop_index("ix_backup_schedules_tenant_id", table_name="backup_schedules")
    op.drop_table("backup_schedules")
