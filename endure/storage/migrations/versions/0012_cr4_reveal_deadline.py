"""Persist CR4 reveal deadlines independently from extrinsic mortality.

Revision ID: 0012_cr4_reveal_deadline
Revises: 0011_weight_emission_confirmation
Create Date: 2026-08-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_cr4_reveal_deadline"
down_revision = "0011_weight_emission_confirmation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "weight_emission_batches",
        sa.Column("cr4_reveal_deadline_block", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("weight_emission_batches", "cr4_reveal_deadline_block")
