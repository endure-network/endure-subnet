"""Persist durable confirmation state for one-shot weight submissions.

Revision ID: 0011_weight_emission_confirmation
Revises: 0010_reveal_count
Create Date: 2026-08-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from alembic.operations import BatchOperations
from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.schema import SchemaItem

revision = "0011_weight_emission_confirmation"
down_revision = "0010_reveal_count"
branch_labels = None
depends_on = None


_ROWS_BACKUP = "_weight_emission_rows_0011_backup"
_BATCHES_TEMP = "_alembic_tmp_weight_emission_batches"


def _sqlite_tables(connection: Connection) -> set[str]:
    return set(inspect(connection).get_table_names())


def _resume_sqlite_batch_rebuild(connection: Connection) -> None:
    tables = _sqlite_tables(connection)
    has_batches = "weight_emission_batches" in tables
    has_temp = _BATCHES_TEMP in tables
    if has_temp and not has_batches:
        op.rename_table(_BATCHES_TEMP, "weight_emission_batches")
    elif has_temp:
        op.drop_table(_BATCHES_TEMP)


def _backup_sqlite_weight_rows(connection: Connection) -> None:
    tables = _sqlite_tables(connection)
    if _ROWS_BACKUP in tables:
        if "weight_emission_rows" in tables:
            op.drop_table("weight_emission_rows")
        return
    op.execute(f"CREATE TABLE {_ROWS_BACKUP} AS SELECT * FROM weight_emission_rows")
    op.drop_table("weight_emission_rows")


def _restore_sqlite_weight_rows(*, with_emitted_check: bool) -> None:
    table_items: list[SchemaItem] = [
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
    ]
    if with_emitted_check:
        table_items.extend(
            (
                sa.CheckConstraint(
                    "emitted = 0 OR weight_u16 IS NOT NULL",
                    name="ck_weight_emission_row_emitted_weight",
                ),
                sa.CheckConstraint(
                    "emitted IN (0, 1)",
                    name="ck_weight_emission_row_emitted_boolean",
                ),
            )
        )
    op.create_table("weight_emission_rows", *table_items)
    op.execute(
        "INSERT INTO weight_emission_rows ("
        "id, batch_id, miner_hotkey, uid, blended_score_text, "
        "weight_norm_precap_text, weight_processed_text, weight_u16, emitted"
        ") SELECT id, batch_id, miner_hotkey, uid, blended_score_text, "
        "weight_norm_precap_text, weight_processed_text, weight_u16, emitted "
        f"FROM {_ROWS_BACKUP}"
    )
    op.drop_table(_ROWS_BACKUP)


def upgrade() -> None:
    connection = op.get_bind()
    sqlite = connection.dialect.name == "sqlite"
    if sqlite:
        _backup_sqlite_weight_rows(connection)
        _resume_sqlite_batch_rebuild(connection)
    batch_columns = {
        column["name"]
        for column in inspect(connection).get_columns("weight_emission_batches")
    }
    if "confirmation_state" not in batch_columns:
        with op.batch_alter_table("weight_emission_batches") as batch:
            _add_confirmation_columns(batch)
    if sqlite:
        _restore_sqlite_weight_rows(with_emitted_check=True)
    else:
        with op.batch_alter_table("weight_emission_rows") as batch:
            batch.create_check_constraint(
                "ck_weight_emission_row_emitted_weight",
                "emitted = 0 OR weight_u16 IS NOT NULL",
            )
            batch.create_check_constraint(
                "ck_weight_emission_row_emitted_boolean",
                "emitted IN (0, 1)",
            )
    if sqlite:
        op.execute(
            "CREATE INDEX IF NOT EXISTS "
            "ix_weight_emission_batches_schema_confirmation "
            "ON weight_emission_batches (schema_id, confirmation_state)"
        )
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_weight_emission_open_schema "
            "ON weight_emission_batches (schema_id) WHERE confirmation_state "
            "IN ('prepared', 'submitted', 'ambiguous')"
        )
    else:
        op.create_index(
            "ix_weight_emission_batches_schema_confirmation",
            "weight_emission_batches",
            ["schema_id", "confirmation_state"],
        )
        op.create_index(
            "uq_weight_emission_open_schema",
            "weight_emission_batches",
            ["schema_id"],
            unique=True,
        )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS ck_weight_emission_batch_insert_confirmed "
        "BEFORE INSERT ON weight_emission_batches "
        "WHEN NEW.confirmation_state = 'confirmed' BEGIN "
        "SELECT RAISE(ABORT, 'confirmed batch must be resolved in place'); "
        "END"
    )
    _create_confirmation_triggers()


def _add_confirmation_columns(batch: BatchOperations) -> None:
    batch.add_column(sa.Column("submission_block", sa.Integer(), nullable=True))
    batch.add_column(sa.Column("confirmation_state", sa.Text(), nullable=True))
    batch.add_column(sa.Column("confirmed_at", sa.Text(), nullable=True))
    batch.add_column(
        sa.Column("confirmation_deadline_block", sa.Integer(), nullable=True)
    )
    batch.add_column(
        sa.Column("baseline_last_update_block", sa.Integer(), nullable=True)
    )
    batch.add_column(sa.Column("period_blocks", sa.Integer(), nullable=True))
    batch.add_column(sa.Column("chain_identity", sa.Text(), nullable=True))
    batch.add_column(sa.Column("netuid", sa.Integer(), nullable=True))
    batch.add_column(sa.Column("validator_uid", sa.Integer(), nullable=True))
    batch.add_column(sa.Column("validator_hotkey", sa.Text(), nullable=True))
    batch.add_column(sa.Column("submission_mode", sa.Text(), nullable=True))
    batch.add_column(sa.Column("intent_hash", sa.Text(), nullable=True))
    batch.add_column(sa.Column("commitment_hash", sa.Text(), nullable=True))
    batch.add_column(sa.Column("reveal_round", sa.Integer(), nullable=True))
    batch.add_column(
        sa.Column("commitment_observed_block", sa.Integer(), nullable=True)
    )
    batch.add_column(
        sa.Column(
            "commitment_observed_last_update_block",
            sa.Integer(),
            nullable=True,
        )
    )
    batch.create_check_constraint(
        "ck_weight_emission_confirmation_state",
        "confirmation_state IS NULL OR confirmation_state IN "
        "('prepared', 'submitted', 'ambiguous', 'confirmed', "
        "'unconfirmed', 'failed')",
    )
    batch.create_check_constraint(
        "ck_weight_emission_submission_block",
        "submission_block IS NULL OR submission_block >= 0",
    )
    batch.create_check_constraint(
        "ck_weight_emission_baseline_block",
        "baseline_last_update_block IS NULL OR baseline_last_update_block >= 0",
    )
    batch.create_check_constraint(
        "ck_weight_emission_period_blocks",
        "period_blocks IS NULL OR period_blocks > 0",
    )
    batch.create_check_constraint(
        "ck_weight_emission_deadline_block",
        "confirmation_deadline_block IS NULL OR confirmation_deadline_block >= 0",
    )
    batch.create_check_constraint(
        "ck_weight_emission_netuid", "netuid IS NULL OR netuid >= 0"
    )
    batch.create_check_constraint(
        "ck_weight_emission_validator_uid",
        "validator_uid IS NULL OR validator_uid >= 0",
    )
    batch.create_check_constraint(
        "ck_weight_emission_submission_mode",
        "submission_mode IS NULL OR submission_mode IN ('direct', 'cr4')",
    )
    batch.create_check_constraint(
        "ck_weight_emission_reveal_round",
        "reveal_round IS NULL OR reveal_round >= 0",
    )
    batch.create_check_constraint(
        "ck_weight_emission_commitment_observed_block",
        "commitment_observed_block IS NULL OR commitment_observed_block >= 0",
    )
    batch.create_check_constraint(
        "ck_weight_emission_commitment_observed_last_update_block",
        "commitment_observed_last_update_block IS NULL OR "
        "commitment_observed_last_update_block >= 0",
    )
    batch.create_check_constraint(
        "ck_weight_emission_open_fields",
        "confirmation_state NOT IN ('prepared', 'submitted', 'ambiguous') "
        "OR (submission_block IS NOT NULL AND "
        "baseline_last_update_block IS NOT NULL AND period_blocks IS NOT NULL "
        "AND confirmation_deadline_block IS NOT NULL "
        "AND chain_identity IS NOT NULL AND netuid IS NOT NULL "
        "AND validator_uid IS NOT NULL AND validator_hotkey IS NOT NULL "
        "AND submission_mode IS NOT NULL AND intent_hash IS NOT NULL)",
    )
    batch.create_check_constraint(
        "ck_weight_emission_resolution_fields",
        "confirmation_state NOT IN ('confirmed', 'unconfirmed') OR "
        "(submission_block IS NOT NULL AND "
        "baseline_last_update_block IS NOT NULL AND period_blocks IS NOT NULL "
        "AND chain_identity IS NOT NULL AND netuid IS NOT NULL "
        "AND validator_uid IS NOT NULL AND validator_hotkey IS NOT NULL "
        "AND submission_mode IS NOT NULL AND intent_hash IS NOT NULL)",
    )
    batch.create_check_constraint(
        "ck_weight_emission_cr4_metadata",
        "(commitment_hash IS NULL AND reveal_round IS NULL) OR "
        "(submission_mode = 'cr4' AND commitment_hash IS NOT NULL "
        "AND reveal_round IS NOT NULL)",
    )
    batch.create_check_constraint(
        "ck_weight_emission_safe_deadline",
        "confirmation_deadline_block IS NULL OR submission_block IS NULL OR "
        "period_blocks IS NULL OR confirmation_deadline_block >= "
        "submission_block + period_blocks AND confirmation_deadline_block <= "
        "submission_block + (2 * period_blocks)",
    )
    batch.create_check_constraint(
        "ck_weight_emission_submission_after_baseline",
        "submission_block IS NULL OR baseline_last_update_block IS NULL OR "
        "submission_block >= baseline_last_update_block",
    )
    batch.create_check_constraint(
        "ck_weight_emission_status_state",
        "(confirmation_state NOT IN ('prepared', 'ambiguous') OR "
        "status = 'error') AND (confirmation_state != 'submitted' OR "
        "status = 'submitted') AND (confirmation_state != 'failed' OR "
        "status IN ('failed', 'error'))",
    )
    batch.create_check_constraint(
        "ck_weight_emission_confirmed_at",
        "CASE WHEN confirmation_state = 'confirmed' THEN confirmed_at IS NOT NULL "
        "ELSE confirmed_at IS NULL END",
    )


def _create_confirmation_triggers() -> None:
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS ck_weight_emission_row_insert_confirmed "
        "BEFORE INSERT ON weight_emission_rows "
        "WHEN EXISTS (SELECT 1 FROM weight_emission_batches "
        "WHERE id = NEW.batch_id AND confirmation_state = 'confirmed') OR ("
        "NEW.emitted IS NOT 0 AND NOT EXISTS ("
        "SELECT 1 FROM weight_emission_batches "
        "WHERE id = NEW.batch_id AND confirmation_state = 'confirmed'"
        ")) BEGIN "
        "SELECT RAISE(ABORT, 'emitted row requires confirmed batch'); "
        "END"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS ck_weight_emission_row_update_confirmed "
        "BEFORE UPDATE ON weight_emission_rows "
        "WHEN (EXISTS (SELECT 1 FROM weight_emission_batches "
        "WHERE id = OLD.batch_id AND confirmation_state = 'confirmed') "
        "AND NOT (OLD.emitted IS 0 AND NEW.emitted IS 1 "
        "AND NEW.id IS OLD.id AND NEW.batch_id IS OLD.batch_id "
        "AND NEW.uid IS OLD.uid AND NEW.miner_hotkey IS OLD.miner_hotkey "
        "AND NEW.blended_score_text IS OLD.blended_score_text "
        "AND NEW.weight_norm_precap_text IS OLD.weight_norm_precap_text "
        "AND NEW.weight_processed_text IS OLD.weight_processed_text "
        "AND NEW.weight_u16 IS OLD.weight_u16)) OR "
        "(OLD.emitted IS NOT 0 AND (NEW.id IS NOT OLD.id "
        "OR NEW.emitted IS NOT OLD.emitted OR NEW.batch_id IS NOT OLD.batch_id "
        "OR NEW.uid IS NOT OLD.uid OR NEW.miner_hotkey IS NOT OLD.miner_hotkey "
        "OR NEW.blended_score_text IS NOT OLD.blended_score_text "
        "OR NEW.weight_norm_precap_text IS NOT OLD.weight_norm_precap_text "
        "OR NEW.weight_processed_text IS NOT OLD.weight_processed_text "
        "OR NEW.weight_u16 IS NOT OLD.weight_u16)) OR ("
        "NEW.batch_id IS NOT OLD.batch_id AND EXISTS ("
        "SELECT 1 FROM weight_emission_batches WHERE id = NEW.batch_id "
        "AND confirmation_state = 'confirmed')) OR ("
        "NEW.emitted IS NOT 0 AND NOT EXISTS ("
        "SELECT 1 FROM weight_emission_batches WHERE id = NEW.batch_id "
        "AND confirmation_state = 'confirmed')) BEGIN "
        "SELECT RAISE(ABORT, 'emitted row requires confirmed batch'); "
        "END"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS ck_weight_emission_row_delete_confirmed "
        "BEFORE DELETE ON weight_emission_rows "
        "WHEN OLD.emitted IS NOT 0 OR EXISTS ("
        "SELECT 1 FROM weight_emission_batches WHERE id = OLD.batch_id "
        "AND confirmation_state = 'confirmed') BEGIN "
        "SELECT RAISE(ABORT, 'confirmed emitted row is immutable'); "
        "END"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS ck_weight_emission_batch_update_confirmed "
        "BEFORE UPDATE ON weight_emission_batches "
        "WHEN OLD.confirmation_state = 'confirmed' BEGIN "
        "SELECT RAISE(ABORT, 'confirmed batch is immutable'); "
        "END"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS ck_weight_emission_batch_delete_confirmed "
        "BEFORE DELETE ON weight_emission_batches "
        "WHEN OLD.confirmation_state = 'confirmed' BEGIN "
        "SELECT RAISE(ABORT, 'confirmed batch is immutable'); "
        "END"
    )


def downgrade() -> None:
    connection = op.get_bind()
    sqlite = connection.dialect.name == "sqlite"
    op.execute("DROP TRIGGER IF EXISTS ck_weight_emission_batch_insert_confirmed")
    op.execute("DROP TRIGGER IF EXISTS ck_weight_emission_batch_update_confirmed")
    op.execute("DROP TRIGGER IF EXISTS ck_weight_emission_batch_delete_confirmed")
    op.execute("DROP TRIGGER IF EXISTS ck_weight_emission_row_delete_confirmed")
    op.execute("DROP TRIGGER IF EXISTS ck_weight_emission_row_update_confirmed")
    op.execute("DROP TRIGGER IF EXISTS ck_weight_emission_row_insert_confirmed")
    op.drop_index(
        "uq_weight_emission_open_schema",
        table_name="weight_emission_batches",
    )
    op.drop_index(
        "ix_weight_emission_batches_schema_confirmation",
        table_name="weight_emission_batches",
    )
    if sqlite:
        _backup_sqlite_weight_rows(connection)
    else:
        with op.batch_alter_table("weight_emission_rows") as batch:
            batch.drop_constraint(
                "ck_weight_emission_row_emitted_boolean", type_="check"
            )
            batch.drop_constraint(
                "ck_weight_emission_row_emitted_weight", type_="check"
            )
    with op.batch_alter_table("weight_emission_batches") as batch:
        batch.drop_constraint("ck_weight_emission_confirmed_at", type_="check")
        batch.drop_constraint("ck_weight_emission_open_fields", type_="check")
        batch.drop_constraint("ck_weight_emission_resolution_fields", type_="check")
        batch.drop_constraint("ck_weight_emission_cr4_metadata", type_="check")
        batch.drop_constraint("ck_weight_emission_safe_deadline", type_="check")
        batch.drop_constraint(
            "ck_weight_emission_submission_after_baseline", type_="check"
        )
        batch.drop_constraint("ck_weight_emission_status_state", type_="check")
        batch.drop_constraint(
            "ck_weight_emission_commitment_observed_last_update_block", type_="check"
        )
        batch.drop_constraint(
            "ck_weight_emission_commitment_observed_block", type_="check"
        )
        batch.drop_constraint("ck_weight_emission_reveal_round", type_="check")
        batch.drop_constraint("ck_weight_emission_submission_mode", type_="check")
        batch.drop_constraint("ck_weight_emission_validator_uid", type_="check")
        batch.drop_constraint("ck_weight_emission_netuid", type_="check")
        batch.drop_constraint("ck_weight_emission_deadline_block", type_="check")
        batch.drop_constraint("ck_weight_emission_period_blocks", type_="check")
        batch.drop_constraint("ck_weight_emission_baseline_block", type_="check")
        batch.drop_constraint("ck_weight_emission_submission_block", type_="check")
        batch.drop_constraint("ck_weight_emission_confirmation_state", type_="check")
        batch.drop_column("commitment_observed_last_update_block")
        batch.drop_column("commitment_observed_block")
        batch.drop_column("reveal_round")
        batch.drop_column("commitment_hash")
        batch.drop_column("intent_hash")
        batch.drop_column("submission_mode")
        batch.drop_column("validator_hotkey")
        batch.drop_column("validator_uid")
        batch.drop_column("netuid")
        batch.drop_column("chain_identity")
        batch.drop_column("period_blocks")
        batch.drop_column("baseline_last_update_block")
        batch.drop_column("confirmation_deadline_block")
        batch.drop_column("confirmed_at")
        batch.drop_column("confirmation_state")
        batch.drop_column("submission_block")
    if sqlite:
        _restore_sqlite_weight_rows(with_emitted_check=False)
