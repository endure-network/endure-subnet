"""Persist consensus-time bundle snapshots for deterministic scoring.

Revision ID: 0008_consensus_bundle_snapshots
Revises: 0007_backfill_assessment_resolution_markers
Create Date: 2026-07-21
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0008_consensus_bundle_snapshots"
down_revision = "0007_backfill_assessment_resolution_markers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS consensus_bundle_snapshots"))
    op.create_table(
        "consensus_bundle_snapshots",
        sa.Column("round_id", sa.Text(), primary_key=True),
        sa.Column("schema_id", sa.Text(), primary_key=True),
        sa.Column("miner_hotkey", sa.Text(), primary_key=True),
        sa.Column("bundle_json", sa.Text(), nullable=False),
        sa.Column("snapshotted_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["round_id", "schema_id"], ["rounds.round_id", "rounds.schema_id"]
        ),
    )
    op.get_bind().execute(
        sa.text(
            """
            INSERT OR IGNORE INTO consensus_bundle_snapshots (
                round_id, schema_id, miner_hotkey, bundle_json, snapshotted_at
            )
            SELECT
                submissions.round_id,
                submissions.schema_id,
                submissions.miner_hotkey,
                submissions.bundle_json,
                :snapshotted_at
            FROM submissions
            JOIN rounds ON
                rounds.round_id = submissions.round_id
                AND rounds.schema_id = submissions.schema_id
            WHERE submissions.verdict = 'accepted'
                AND rounds.state = 'closed'
            """
        ),
        {"snapshotted_at": datetime.now(UTC).isoformat()},
    )


def downgrade() -> None:
    op.drop_table("consensus_bundle_snapshots")
