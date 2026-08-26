"""Add assessment horizon resolution markers.

Revision ID: 0006_assessment_horizon_resolutions
Revises: 0005_generic_assessment_tables
Create Date: 2026-07-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_assessment_horizon_resolutions"
down_revision = "0005_generic_assessment_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assessment_horizon_resolutions",
        sa.Column("round_id", sa.Text(), primary_key=True),
        sa.Column("schema_id", sa.Text(), primary_key=True),
        sa.Column("horizon_value", sa.Integer(), primary_key=True),
        sa.Column("resolved_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["round_id", "schema_id"], ["rounds.round_id", "rounds.schema_id"]
        ),
    )
    op.create_index(
        "ix_assessment_horizon_resolutions_schema",
        "assessment_horizon_resolutions",
        ["schema_id", "horizon_value"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_assessment_horizon_resolutions_schema",
        table_name="assessment_horizon_resolutions",
    )
    op.drop_table("assessment_horizon_resolutions")
