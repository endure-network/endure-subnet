"""Backfill assessment horizon markers and optimize resolved target reads.

Revision ID: 0007_backfill_assessment_resolution_markers
Revises: 0006_assessment_horizon_resolutions
Create Date: 2026-07-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_backfill_assessment_resolution_markers"
down_revision = "0006_assessment_horizon_resolutions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        "ix_assessment_horizon_resolutions_schema",
        table_name="assessment_horizon_resolutions",
    )
    op.create_index(
        "ix_assessment_realized_schema_horizon_status_target_round_output",
        "assessment_realized_targets",
        [
            "schema_id",
            "horizon_value",
            "target_kind",
            "status",
            "output",
            "target_id",
            "round_id",
            "value_text",
        ],
    )
    op.execute(
        sa.text(
            """
            INSERT OR IGNORE INTO assessment_horizon_resolutions (
                round_id, schema_id, horizon_value, resolved_at
            )
            SELECT
                round_id,
                schema_id,
                horizon_value,
                MIN(fetched_at) AS resolved_at
            FROM assessment_realized_targets
            WHERE status = 'resolved'
            GROUP BY round_id, schema_id, horizon_value
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_assessment_realized_schema_horizon_status_target_round_output",
        table_name="assessment_realized_targets",
    )
    op.create_index(
        "ix_assessment_horizon_resolutions_schema",
        "assessment_horizon_resolutions",
        ["schema_id", "horizon_value"],
    )
