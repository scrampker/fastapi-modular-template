"""Add search_vector column and GIN index to items table.

Revision ID: 0001_add_items_search_vector
Revises:
Create Date: 2026-04-07

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0001_add_items_search_vector"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgresql() -> bool:
    """Return True when the connected database is PostgreSQL."""
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # Add the search_vector column to the items table.
    # Using Text (plain string) so SQLite is also supported.
    op.add_column(
        "items",
        sa.Column("search_vector", sa.Text(), nullable=True),
    )

    # Back-fill search_vector from existing name + description.
    op.execute(
        """
        UPDATE items
        SET search_vector = TRIM(
            COALESCE(name, '') || ' ' || COALESCE(description, '')
        )
        """
    )

    if _is_postgresql():
        # Create a GIN index on the tsvector expression for fast FTS queries.
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_items_search_vector_gin
            ON items
            USING gin (to_tsvector('english', coalesce(search_vector, '')))
            """
        )


def downgrade() -> None:
    if _is_postgresql():
        op.execute("DROP INDEX IF EXISTS ix_items_search_vector_gin")

    op.drop_column("items", "search_vector")
