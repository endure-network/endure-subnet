"""Append-only per-round score/EMA history for dashboards and audit.

Revision ID: 0003_score_history
Revises: 0002_commit_count
Create Date: 2026-06-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_score_history"
down_revision = "0002_commit_count"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "score_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("round_id", sa.Text(), nullable=False),
        sa.Column("schema_id", sa.Text(), nullable=False),
        sa.Column("miner_hotkey", sa.Text(), nullable=False),
        sa.Column("horizon_trading_days", sa.Integer(), nullable=False),
        sa.Column("round_score_text", sa.Text(), nullable=False),
        sa.Column("ema_after_text", sa.Text(), nullable=False),
        sa.Column("scored_at", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "round_id", "schema_id", "miner_hotkey", "horizon_trading_days"
        ),
        sa.ForeignKeyConstraint(
            ["round_id", "schema_id"], ["rounds.round_id", "rounds.schema_id"]
        ),
    )


def downgrade() -> None:
    op.drop_table("score_history")
