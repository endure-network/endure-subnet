"""Persist the per-round public-read embargo boundary.

Revision ID: 0015_round_publication_embargo
Revises: 0014_drop_kre_tables
Create Date: 2026-08-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_round_publication_embargo"
down_revision = "0014_drop_kre_tables"
branch_labels = None
depends_on = None

_COLUMN = "publication_available_at"


def _round_columns() -> dict[str, dict[str, object]]:
    return {
        str(column["name"]): column
        for column in sa.inspect(op.get_bind()).get_columns("rounds")
    }


def upgrade() -> None:
    """Backfill legacy visibility while making every new boundary explicit."""
    columns = _round_columns()
    if _COLUMN not in columns:
        op.add_column(
            "rounds",
            sa.Column(
                _COLUMN,
                sa.Text(),
                nullable=False,
                server_default=sa.text("''"),
            ),
        )
    op.execute(
        sa.text(
            "UPDATE rounds SET publication_available_at = reveal_close_at "
            "WHERE publication_available_at IS NULL "
            "OR publication_available_at = ''"
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER IF NOT EXISTS rounds_publication_available_default
            AFTER INSERT ON rounds
            WHEN NEW.publication_available_at = ''
            BEGIN
                UPDATE rounds
                SET publication_available_at = NEW.reveal_close_at
                WHERE round_id = NEW.round_id AND schema_id = NEW.schema_id;
            END
            """
        )
    )


def downgrade() -> None:
    if _COLUMN in _round_columns():
        op.execute(
            sa.text("DROP TRIGGER IF EXISTS rounds_publication_available_default")
        )
        op.drop_column("rounds", _COLUMN)
