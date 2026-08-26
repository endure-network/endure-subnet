"""Hot-query indexes for the dashboard read paths.

Revision ID: 0004_hot_query_indexes
Revises: 0003_score_history
Create Date: 2026-06-11
"""

from __future__ import annotations

from alembic import op

revision = "0004_hot_query_indexes"
down_revision = "0003_score_history"
branch_labels = None
depends_on = None

_INDEXES = (
    ("ix_rounds_schema_state", "rounds", ["schema_id", "state"]),
    (
        "ix_submissions_round_verdict",
        "submissions",
        ["round_id", "schema_id", "verdict"],
    ),
    ("ix_miner_score_state_schema", "miner_score_state", ["schema_id"]),
    (
        "ix_realized_outcomes_schema_ticker",
        "realized_outcomes",
        ["schema_id", "ticker"],
    ),
    ("ix_consensus_schema_ticker", "consensus_aggregates", ["schema_id", "ticker"]),
    # NOTE: ix_score_history_schema_miner (schema_id, miner_hotkey) is not hit by
    # any current read — score_history_for_round filters on (round_id, schema_id),
    # which the table's UniqueConstraint(round_id, schema_id, miner_hotkey,
    # horizon) already serves via its leading prefix. Retained as-is to avoid
    # editing an applied migration; a dedicated migration should drop it.
    ("ix_score_history_schema_miner", "score_history", ["schema_id", "miner_hotkey"]),
)


def upgrade() -> None:
    for name, table, columns in _INDEXES:
        op.create_index(name, table, columns)


def downgrade() -> None:
    for name, table, _columns in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
