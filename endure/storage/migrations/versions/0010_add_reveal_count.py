"""Add submissions.reveal_count for restart-surviving reveal rate limits (spec §6).

Revision ID: 0010_reveal_count
Revises: 0009_weight_emissions
Create Date: 2026-08-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_reveal_count"
down_revision = "0009_weight_emissions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "submissions",
        sa.Column(
            "reveal_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("submissions") as batch:
        batch.drop_column("reveal_count")
