"""Add submissions.commit_count for restart-surviving rate limits (spec §6).

Revision ID: 0002_commit_count
Revises: 0001_kre_loop_tables
Create Date: 2026-06-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_commit_count"
down_revision = "0001_kre_loop_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "submissions",
        sa.Column(
            "commit_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("submissions") as batch:
        batch.drop_column("commit_count")
