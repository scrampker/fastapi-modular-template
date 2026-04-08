"""HIPAA security patterns: login_attempts table, session_version on users.

Revision ID: 0002_hipaa_security_patterns
Revises: 0001_add_items_search_vector
Create Date: 2026-04-07

Changes:
  - Add ``login_attempts`` table for failed-login tracking and account lockout.
  - Add ``session_version`` (integer, default 0) to the ``users`` table for
    forced re-auth when an admin deactivates a user or resets a password.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0002_hipaa_security_patterns"
down_revision: Union[str, None] = "0001_add_items_search_vector"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. login_attempts — one row per login attempt (success or failure)
    # ------------------------------------------------------------------
    op.create_table(
        "login_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("ip_address", sa.String(45), nullable=False, server_default="unknown"),
        sa.Column(
            "attempted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_index("ix_login_attempts_email", "login_attempts", ["email"])
    op.create_index("ix_login_attempts_attempted_at", "login_attempts", ["attempted_at"])

    # ------------------------------------------------------------------
    # 2. session_version on users — default 0, increment to force re-auth
    # ------------------------------------------------------------------
    op.add_column(
        "users",
        sa.Column("session_version", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("users", "session_version")
    op.drop_index("ix_login_attempts_attempted_at", table_name="login_attempts")
    op.drop_index("ix_login_attempts_email", table_name="login_attempts")
    op.drop_table("login_attempts")
