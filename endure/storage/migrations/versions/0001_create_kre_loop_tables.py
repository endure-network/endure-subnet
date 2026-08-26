"""Create the seven KRE-loop tables (KRE first-loop spec §5).

Revision ID: 0001_kre_loop_tables
Revises:
Create Date: 2026-06-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_kre_loop_tables"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rounds",
        sa.Column("round_id", sa.Text(), nullable=False),
        sa.Column("schema_id", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column(
            "universe_stale", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "degraded", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("commit_open_at", sa.Text(), nullable=False),
        sa.Column("commit_close_at", sa.Text(), nullable=False),
        sa.Column("reveal_open_at", sa.Text(), nullable=False),
        sa.Column("reveal_close_at", sa.Text(), nullable=False),
        sa.Column("t0_close_at", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("round_id", "schema_id", name="pk_rounds"),
    )
    op.create_table(
        "universe_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("round_id", sa.Text(), nullable=False),
        sa.Column("schema_id", sa.Text(), nullable=False),
        sa.Column("tickers_json", sa.Text(), nullable=False),
        sa.Column("source_hash", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_universe_snapshots"),
        sa.UniqueConstraint(
            "round_id", "schema_id", name="uq_universe_snapshots_round_id"
        ),
    )
    op.create_table(
        "submissions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("round_id", sa.Text(), nullable=False),
        sa.Column("schema_id", sa.Text(), nullable=False),
        sa.Column("miner_hotkey", sa.Text(), nullable=False),
        sa.Column("commit_hash", sa.Text(), nullable=True),
        sa.Column("nonce_hex", sa.Text(), nullable=True),
        sa.Column("committed_at", sa.Text(), nullable=True),
        sa.Column("revealed_at", sa.Text(), nullable=True),
        sa.Column("bundle_json", sa.Text(), nullable=True),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("rejection_code", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_submissions"),
        sa.UniqueConstraint(
            "round_id", "schema_id", "miner_hotkey", name="uq_submissions_round_id"
        ),
        sa.ForeignKeyConstraint(
            ["round_id", "schema_id"],
            ["rounds.round_id", "rounds.schema_id"],
            name="fk_submissions_round_id_rounds",
        ),
    )
    op.create_table(
        "realized_outcomes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("round_id", sa.Text(), nullable=False),
        sa.Column("schema_id", sa.Text(), nullable=False),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("horizon_trading_days", sa.Integer(), nullable=False),
        sa.Column("realized_return_bps", sa.Integer(), nullable=True),
        sa.Column("realized_relative_return_vs_kre_bps", sa.Integer(), nullable=True),
        sa.Column("realized_max_drawdown_bps", sa.Integer(), nullable=True),
        sa.Column("stress_event", sa.Boolean(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("provider_payload_hash", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_realized_outcomes"),
        sa.UniqueConstraint(
            "round_id",
            "schema_id",
            "ticker",
            "horizon_trading_days",
            name="uq_realized_outcomes_round_id",
        ),
        sa.ForeignKeyConstraint(
            ["round_id", "schema_id"],
            ["rounds.round_id", "rounds.schema_id"],
            name="fk_realized_outcomes_round_id_rounds",
        ),
    )
    op.create_table(
        "scoring_records",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("round_id", sa.Text(), nullable=False),
        sa.Column("schema_id", sa.Text(), nullable=False),
        sa.Column("miner_hotkey", sa.Text(), nullable=False),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("horizon_trading_days", sa.Integer(), nullable=False),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("score_text", sa.Text(), nullable=False),
        sa.Column("scored_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_scoring_records"),
        sa.UniqueConstraint(
            "round_id",
            "schema_id",
            "miner_hotkey",
            "ticker",
            "horizon_trading_days",
            "field",
            name="uq_scoring_records_round_id",
        ),
        sa.ForeignKeyConstraint(
            ["round_id", "schema_id"],
            ["rounds.round_id", "rounds.schema_id"],
            name="fk_scoring_records_round_id_rounds",
        ),
    )
    op.create_table(
        "miner_score_state",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("miner_hotkey", sa.Text(), nullable=False),
        sa.Column("schema_id", sa.Text(), nullable=False),
        sa.Column("horizon_trading_days", sa.Integer(), nullable=False),
        sa.Column("ema_text", sa.Text(), nullable=False),
        sa.Column(
            "resolved_rounds", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_miner_score_state"),
        sa.UniqueConstraint(
            "miner_hotkey",
            "schema_id",
            "horizon_trading_days",
            name="uq_miner_score_state_miner_hotkey",
        ),
    )
    op.create_table(
        "consensus_aggregates",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("round_id", sa.Text(), nullable=False),
        sa.Column("schema_id", sa.Text(), nullable=False),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("horizon_trading_days", sa.Integer(), nullable=False),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("value_text", sa.Text(), nullable=False),
        sa.Column("dispersion_text", sa.Text(), nullable=False),
        sa.Column("n_submitters", sa.Integer(), nullable=False),
        sa.Column("computed_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_consensus_aggregates"),
        sa.UniqueConstraint(
            "round_id",
            "schema_id",
            "ticker",
            "horizon_trading_days",
            "field",
            name="uq_consensus_aggregates_round_id",
        ),
        sa.ForeignKeyConstraint(
            ["round_id", "schema_id"],
            ["rounds.round_id", "rounds.schema_id"],
            name="fk_consensus_aggregates_round_id_rounds",
        ),
    )


def downgrade() -> None:
    op.drop_table("consensus_aggregates")
    op.drop_table("miner_score_state")
    op.drop_table("scoring_records")
    op.drop_table("realized_outcomes")
    op.drop_table("submissions")
    op.drop_table("universe_snapshots")
    op.drop_table("rounds")
