"""Reconcile CR4 deadlines and persist protocol-scoped startup fences.

Revision ID: 0013_weight_emission_reconciliation
Revises: 0012_cr4_reveal_deadline
Create Date: 2026-08-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_weight_emission_reconciliation"
down_revision = "0012_cr4_reveal_deadline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "weight_emission_batches",
        sa.Column("reveal_scan_cursor_block", sa.Integer(), nullable=True),
    )
    op.create_table(
        "weight_emission_startup_fences",
        sa.Column("schema_id", sa.Text(), nullable=False),
        sa.Column("protocol_version_key", sa.Integer(), nullable=False),
        sa.Column("fence_block", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "protocol_version_key >= 0", name="ck_startup_fence_protocol_key"
        ),
        sa.CheckConstraint("fence_block >= 0", name="ck_startup_fence_block"),
        sa.PrimaryKeyConstraint("schema_id", "protocol_version_key"),
    )
    op.execute(
        "UPDATE weight_emission_batches "
        "SET cr4_reveal_deadline_block = confirmation_deadline_block "
        "WHERE submission_mode = 'cr4' "
        "AND confirmation_state IN ('prepared', 'submitted', 'ambiguous') "
        "AND cr4_reveal_deadline_block IS NULL"
    )


def downgrade() -> None:
    op.drop_table("weight_emission_startup_fences")
    op.drop_column("weight_emission_batches", "reveal_scan_cursor_block")
