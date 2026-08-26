"""Storage tables for the round and assessment spine (spec §5)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import UniqueConstraint, create_engine, inspect

import endure.storage.tables  # noqa: F401  — registers tables on metadata
from endure.storage.migrations.metadata import metadata

EXPECTED_TABLES = {
    "assessment_consensus",
    "assessment_horizon_resolutions",
    "assessment_miner_score_state",
    "assessment_output_scores",
    "assessment_realized_targets",
    "assessment_score_history",
    "rounds",
    "universe_snapshots",
    "submissions",
    "consensus_bundle_snapshots",
    "weight_emission_batches",
    "weight_emission_rows",
    "weight_emission_startup_fences",
}

COORDINATE_COLUMNS = {
    "target_kind",
    "target_id",
    "horizon_kind",
    "horizon_value",
    "output",
}


def test_all_model_tables_are_registered_on_metadata() -> None:
    assert set(metadata.tables) == EXPECTED_TABLES


def test_decimal_values_are_stored_as_text_columns() -> None:
    score_columns = {
        c.name for c in metadata.tables["assessment_output_scores"].columns
    }
    state_columns = {
        c.name for c in metadata.tables["assessment_miner_score_state"].columns
    }

    assert "score_text" in score_columns
    assert "ema_text" in state_columns


def test_weight_emission_table_models_durable_open_attempts() -> None:
    table = metadata.tables["weight_emission_batches"]
    column_names = {column.name for column in table.columns}
    open_indexes = [
        index
        for index in table.indexes
        if index.name == "uq_weight_emission_open_schema"
    ]

    assert {
        "baseline_last_update_block",
        "period_blocks",
        "chain_identity",
        "netuid",
        "validator_uid",
        "validator_hotkey",
        "submission_mode",
        "intent_hash",
        "commitment_hash",
        "reveal_round",
        "commitment_observed_block",
        "commitment_observed_last_update_block",
    } <= column_names
    assert len(open_indexes) == 1
    assert open_indexes[0].unique is True


def test_submissions_are_unique_per_round_schema_and_miner() -> None:
    table = metadata.tables["submissions"]
    unique_sets = [
        {column.name for column in constraint.columns}
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    ]

    assert {"round_id", "schema_id", "miner_hotkey"} in unique_sets


def test_generic_assessment_tables_use_coordinate_keys_not_ticker_axes() -> None:
    coordinate_tables = {
        "assessment_consensus",
        "assessment_realized_targets",
        "assessment_output_scores",
        "assessment_miner_score_state",
        "assessment_score_history",
    }

    for table_name in coordinate_tables:
        column_names = {c.name for c in metadata.tables[table_name].columns}
        assert COORDINATE_COLUMNS <= column_names
        assert {"ticker", "field", "horizon_trading_days"}.isdisjoint(column_names)


def test_generic_assessment_rows_are_unique_per_coordinate() -> None:
    table = metadata.tables["assessment_output_scores"]
    unique_sets = [
        {column.name for column in constraint.columns}
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    ]

    assert {
        "round_id",
        "schema_id",
        "miner_hotkey",
        *COORDINATE_COLUMNS,
    } in unique_sets


def test_migration_creates_exactly_the_model_tables(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    database_path = tmp_path / "endure-tables.db"

    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option(
        "script_location", str(repo_root / "endure/storage/migrations")
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path}")
    inspector = inspect(engine)
    created = set(inspector.get_table_names()) - {"alembic_version"}

    assert created == EXPECTED_TABLES
    for table_name in EXPECTED_TABLES:
        migrated_columns = {c["name"] for c in inspector.get_columns(table_name)}
        model_columns = {c.name for c in metadata.tables[table_name].columns}
        assert migrated_columns == model_columns, table_name
