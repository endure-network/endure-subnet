"""Store each revealed round's pool tie seed.

Revision ID: 0016_pool_tie_seeds
Revises: 0015_round_publication_embargo
Create Date: 2026-11-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_pool_tie_seeds"
down_revision = "0015_round_publication_embargo"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pool_tie_seeds",
        sa.Column("round_id", sa.Text(), primary_key=True),
        sa.Column("schema_id", sa.Text(), primary_key=True),
        sa.Column("seed", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["round_id", "schema_id"], ["rounds.round_id", "rounds.schema_id"]
        ),
    )


def downgrade() -> None:
    op.drop_table("pool_tie_seeds")
