"""Drop the five KRE-only tables and their indexes.

Revision ID: 0014_drop_kre_tables
Revises: 0013_weight_emission_reconciliation
Create Date: 2026-08-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_drop_kre_tables"
down_revision = "0013_weight_emission_reconciliation"
branch_labels = None
depends_on = None

# (index, table, columns) as 0004_hot_query_indexes created them. Indexes drop
# before their tables so a partial failure can never leave an orphan.
_KRE_INDEXES: tuple[tuple[str, str, list[str]], ...] = (
    ("ix_miner_score_state_schema", "miner_score_state", ["schema_id"]),
    (
        "ix_realized_outcomes_schema_ticker",
        "realized_outcomes",
        ["schema_id", "ticker"],
    ),
    ("ix_consensus_schema_ticker", "consensus_aggregates", ["schema_id", "ticker"]),
    ("ix_score_history_schema_miner", "score_history", ["schema_id", "miner_hotkey"]),
)

# Children first: every KRE table except miner_score_state carries a composite
# foreign key onto rounds, and dropping them in this order keeps the sequence
# valid under enforced foreign keys.
_KRE_TABLES: tuple[str, ...] = (
    "consensus_aggregates",
    "score_history",
    "miner_score_state",
    "scoring_records",
    "realized_outcomes",
)


def _live_schema() -> tuple[set[str], dict[str, set[str]]]:
    """Snapshot table and index names once, before this revision emits any DDL.

    SQLite runs migrations with non-transactional DDL, so a failure part-way
    through leaves the completed drops on disk while the version stays behind.
    Both directions therefore re-read the live schema and skip work already done,
    which makes a retry after a partial run converge instead of crashing on a
    missing index or table.
    """
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    indexes = {
        table: {index["name"] for index in inspector.get_indexes(table)}
        for table in tables
    }
    return tables, indexes


def upgrade() -> None:
    tables, indexes = _live_schema()
    for index_name, table_name, _columns in _KRE_INDEXES:
        if index_name in indexes.get(table_name, frozenset()):
            op.drop_index(index_name, table_name=table_name)
    for table_name in _KRE_TABLES:
        if table_name in tables:
            op.drop_table(table_name)


def _create_table_if_absent(
    existing: set[str], table_name: str, *columns: sa.schema.SchemaItem
) -> None:
    if table_name not in existing:
        op.create_table(table_name, *columns)


def downgrade() -> None:
    """Recreate the prior table and index shapes; KRE row data is not recoverable."""
    tables, indexes = _live_schema()
    _create_table_if_absent(
        tables,
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
    _create_table_if_absent(
        tables,
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
    _create_table_if_absent(
        tables,
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
    _create_table_if_absent(
        tables,
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
    _create_table_if_absent(
        tables,
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
    for index_name, table_name, columns in _KRE_INDEXES:
        if index_name not in indexes.get(table_name, frozenset()):
            op.create_index(index_name, table_name, columns)
