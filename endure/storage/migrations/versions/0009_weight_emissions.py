"""Weight-emission audit trail (fairness-deltas spec §2).

Revision ID: 0009_weight_emissions
Revises: 0008_consensus_bundle_snapshots
Create Date: 2026-07-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_weight_emissions"
down_revision = "0008_consensus_bundle_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "weight_emission_batches",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("schema_id", sa.Text(), nullable=False),
        sa.Column("round_id", sa.Text(), nullable=True),
        sa.Column("emitted_at", sa.Text(), nullable=False),
        sa.Column("block", sa.Integer(), nullable=True),
        sa.Column("min_allowed_weights", sa.Integer(), nullable=True),
        sa.Column("max_weight_limit_text", sa.Text(), nullable=True),
        sa.Column("metagraph_size", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
    )
    op.create_table(
        "weight_emission_rows",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("miner_hotkey", sa.Text(), nullable=False),
        sa.Column("uid", sa.Integer(), nullable=False),
        sa.Column("blended_score_text", sa.Text(), nullable=True),
        sa.Column("weight_norm_precap_text", sa.Text(), nullable=True),
        sa.Column("weight_processed_text", sa.Text(), nullable=False),
        sa.Column("weight_u16", sa.Integer(), nullable=True),
        sa.Column("emitted", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.ForeignKeyConstraint(["batch_id"], ["weight_emission_batches.id"]),
        sa.UniqueConstraint("batch_id", "uid"),
    )


def downgrade() -> None:
    op.drop_table("weight_emission_rows")
    op.drop_table("weight_emission_batches")
