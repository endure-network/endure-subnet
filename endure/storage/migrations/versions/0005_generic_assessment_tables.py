"""Generic assessment-coordinate tables for Forge lending activation.

Revision ID: 0005_generic_assessment_tables
Revises: 0004_hot_query_indexes
Create Date: 2026-06-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_generic_assessment_tables"
down_revision = "0004_hot_query_indexes"
branch_labels = None
depends_on = None

_COORDINATE_NAMES = (
    "target_kind",
    "target_id",
    "horizon_kind",
    "horizon_value",
    "output",
)


def _round_columns() -> tuple[sa.Column[str], sa.Column[str]]:
    return (
        sa.Column("round_id", sa.Text(), nullable=False),
        sa.Column("schema_id", sa.Text(), nullable=False),
    )


def _round_fk() -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["round_id", "schema_id"], ["rounds.round_id", "rounds.schema_id"]
    )


def _coordinate_columns() -> tuple[sa.Column[str] | sa.Column[int], ...]:
    return (
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Text(), nullable=False),
        sa.Column("horizon_kind", sa.Text(), nullable=False),
        sa.Column("horizon_value", sa.Integer(), nullable=False),
        sa.Column("output", sa.Text(), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "assessment_consensus",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        *_round_columns(),
        *_coordinate_columns(),
        sa.Column("value_text", sa.Text(), nullable=False),
        sa.Column("dispersion_text", sa.Text(), nullable=False),
        sa.Column("n_submitters", sa.Integer(), nullable=False),
        sa.Column("computed_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("round_id", "schema_id", *_COORDINATE_NAMES),
        _round_fk(),
    )
    op.create_table(
        "assessment_realized_targets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        *_round_columns(),
        *_coordinate_columns(),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("provider_payload_hash", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("round_id", "schema_id", *_COORDINATE_NAMES),
        _round_fk(),
    )
    op.create_table(
        "assessment_output_scores",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        *_round_columns(),
        sa.Column("miner_hotkey", sa.Text(), nullable=False),
        *_coordinate_columns(),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("score_text", sa.Text(), nullable=False),
        sa.Column("scored_at", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "round_id", "schema_id", "miner_hotkey", *_COORDINATE_NAMES
        ),
        _round_fk(),
    )
    op.create_table(
        "assessment_miner_score_state",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("miner_hotkey", sa.Text(), nullable=False),
        sa.Column("schema_id", sa.Text(), nullable=False),
        *_coordinate_columns(),
        sa.Column("ema_text", sa.Text(), nullable=False),
        sa.Column(
            "resolved_rounds", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("miner_hotkey", "schema_id", *_COORDINATE_NAMES),
    )
    op.create_table(
        "assessment_score_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        *_round_columns(),
        sa.Column("miner_hotkey", sa.Text(), nullable=False),
        *_coordinate_columns(),
        sa.Column("round_score_text", sa.Text(), nullable=False),
        sa.Column("ema_after_text", sa.Text(), nullable=False),
        sa.Column("scored_at", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "round_id", "schema_id", "miner_hotkey", *_COORDINATE_NAMES
        ),
        _round_fk(),
    )
    op.create_index(
        "ix_assessment_consensus_schema_target",
        "assessment_consensus",
        ["schema_id", "target_kind", "target_id"],
    )
    op.create_index(
        "ix_assessment_realized_schema_target",
        "assessment_realized_targets",
        ["schema_id", "target_kind", "target_id"],
    )
    op.create_index(
        "ix_assessment_scores_schema_miner",
        "assessment_output_scores",
        ["schema_id", "miner_hotkey"],
    )
    op.create_index(
        "ix_assessment_state_schema_miner",
        "assessment_miner_score_state",
        ["schema_id", "miner_hotkey"],
    )
    op.create_index(
        "ix_assessment_history_schema_miner",
        "assessment_score_history",
        ["schema_id", "miner_hotkey"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_assessment_history_schema_miner", table_name="assessment_score_history"
    )
    op.drop_index(
        "ix_assessment_state_schema_miner", table_name="assessment_miner_score_state"
    )
    op.drop_index(
        "ix_assessment_scores_schema_miner", table_name="assessment_output_scores"
    )
    op.drop_index(
        "ix_assessment_realized_schema_target",
        table_name="assessment_realized_targets",
    )
    op.drop_index(
        "ix_assessment_consensus_schema_target", table_name="assessment_consensus"
    )
    op.drop_table("assessment_score_history")
    op.drop_table("assessment_miner_score_state")
    op.drop_table("assessment_output_scores")
    op.drop_table("assessment_realized_targets")
    op.drop_table("assessment_consensus")
