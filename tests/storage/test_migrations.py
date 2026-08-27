from __future__ import annotations

import stat
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.operations import Operations
from sqlalchemy import (
    MetaData,
    Table,
    create_engine,
    event,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from endure.assessment.coordinates import (
    AssessmentCoordinate,
    AssessmentEmaState,
    AssessmentRealizedTarget,
)
from endure.assessment.schemas.subnet_alpha_risk import RISK_SCHEMA_ID, RiskOutput
from endure.storage.migrations.metadata import metadata
from endure.storage.repository import Storage

PREVIOUS_HEAD = "0013_weight_emission_reconciliation"

DROPPED_TABLES = (
    "realized_outcomes",
    "scoring_records",
    "miner_score_state",
    "score_history",
    "consensus_aggregates",
)

RETAINED_TABLES = (
    "rounds",
    "universe_snapshots",
    "submissions",
    "consensus_bundle_snapshots",
    "assessment_consensus",
    "assessment_realized_targets",
    "assessment_horizon_resolutions",
    "assessment_output_scores",
    "assessment_miner_score_state",
    "assessment_score_history",
    "weight_emission_batches",
    "weight_emission_rows",
    "weight_emission_startup_fences",
)


def _alembic_config(tmp_path: Path, name: str) -> tuple[Config, Engine]:
    repo_root = Path(__file__).resolve().parents[2]
    database_path = tmp_path / f"{name}.db"
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option(
        "script_location", str(repo_root / "endure/storage/migrations")
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    return config, create_engine(f"sqlite:///{database_path}")


def test_storage_metadata_registers_round_tables() -> None:
    from endure.storage import tables

    assert tables.rounds.name == "rounds"
    assert "rounds" in metadata.tables


def test_storage_metadata_defines_naming_convention() -> None:
    assert set(metadata.naming_convention) == {"ix", "uq", "ck", "fk", "pk"}


def test_alembic_default_sqlalchemy_url_matches_repo_default() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    config = Config(str(repo_root / "alembic.ini"))

    assert config.get_main_option("sqlalchemy.url") == "sqlite:///var/endure.db"


def test_alembic_upgrade_head_runs_against_temp_sqlite_db(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    database_path = tmp_path / "endure-test.db"

    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option(
        "script_location", str(repo_root / "endure/storage/migrations")
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")

    command.upgrade(config, "head")

    assert database_path.exists()
    assert stat.S_IMODE(database_path.stat().st_mode) == 0o600


def test_alembic_upgrade_head_creates_missing_sqlite_parent_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    monkeypatch.chdir(tmp_path)

    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option(
        "script_location", str(repo_root / "endure/storage/migrations")
    )
    config.set_main_option("sqlalchemy.url", "sqlite:///var/endure.db")

    assert not (tmp_path / "var").exists()

    command.upgrade(config, "head")

    assert (tmp_path / "var" / "endure.db").exists()
    assert stat.S_IMODE((tmp_path / "var").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "var" / "endure.db").stat().st_mode) == 0o600


def test_alembic_downgrade_base_then_upgrade_head_round_trips(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    database_path = tmp_path / "endure-round-trip.db"

    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option(
        "script_location", str(repo_root / "endure/storage/migrations")
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")

    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = create_engine(f"sqlite:///{database_path}")
    created = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert created == set()

    command.upgrade(config, "head")
    created = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert "assessment_consensus" in created


def test_0012_adds_cr4_reveal_deadline_without_rewriting_history(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    database_path = tmp_path / "endure-cr4-deadline.db"
    url = f"sqlite:///{database_path}"
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option(
        "script_location", str(repo_root / "endure/storage/migrations")
    )
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0011_weight_emission_confirmation")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO weight_emission_batches "
                "(schema_id, emitted_at, metagraph_size, status) "
                "VALUES ('legacy', '2026-08-19T00:00:00+00:00', 1, 'failed')"
            )
        )

    command.upgrade(config, "head")

    columns = {
        column["name"]
        for column in inspect(engine).get_columns("weight_emission_batches")
    }
    with engine.connect() as connection:
        reveal_deadline = connection.execute(
            text(
                "SELECT cr4_reveal_deadline_block FROM weight_emission_batches "
                "WHERE schema_id = 'legacy'"
            )
        ).scalar_one()
    assert "cr4_reveal_deadline_block" in columns
    assert reveal_deadline is None


def test_head_reconciles_open_cr4_rows_missing_reveal_deadlines(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    database_path = tmp_path / "endure-cr4-reconcile.db"
    url = f"sqlite:///{database_path}"
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option(
        "script_location", str(repo_root / "endure/storage/migrations")
    )
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0012_cr4_reveal_deadline")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO weight_emission_batches "
                "(schema_id, emitted_at, metagraph_size, status, submission_block, "
                "confirmation_state, confirmation_deadline_block, "
                "baseline_last_update_block, period_blocks, chain_identity, netuid, "
                "validator_uid, validator_hotkey, submission_mode, intent_hash) "
                "VALUES ('risk.v1.subnet_alpha', '2026-08-19T00:00:00+00:00', 1, "
                "'submitted', 100, 'submitted', 240, 90, 128, 'genesis-a', 1, 0, "
                "'validator-hotkey', 'cr4', '26:abc')"
            )
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        reveal_deadline, scan_cursor = connection.execute(
            text(
                "SELECT cr4_reveal_deadline_block, reveal_scan_cursor_block "
                "FROM weight_emission_batches"
            )
        ).one()
    assert reveal_deadline == 240
    assert scan_cursor is None


def test_0011_preserves_legacy_weight_emissions_without_confirmation_state(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    database_path = tmp_path / "endure-weight-emission-upgrade.db"
    url = f"sqlite:///{database_path}"
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option(
        "script_location", str(repo_root / "endure/storage/migrations")
    )
    config.set_main_option("sqlalchemy.url", url)

    command.upgrade(config, "0010_reveal_count")
    engine = create_engine(url)
    with engine.begin() as connection:
        inserted_batch = connection.execute(
            text(
                """
                INSERT INTO weight_emission_batches (
                    schema_id, round_id, emitted_at, block, min_allowed_weights,
                    max_weight_limit_text, metagraph_size, status
                ) VALUES (
                    :schema_id, NULL, :emitted_at, 10, 1, '1', 1, 'submitted'
                )
                """
            ),
            {"schema_id": RISK_SCHEMA_ID, "emitted_at": "2026-08-09T00:00:00+00:00"},
        )
        batch_id = inserted_batch.lastrowid
        assert batch_id is not None
        connection.execute(
            text(
                """
                INSERT INTO weight_emission_rows (
                    batch_id, miner_hotkey, uid, blended_score_text,
                    weight_norm_precap_text, weight_processed_text,
                    weight_u16, emitted
                ) VALUES (
                    :batch_id, 'miner-a', 0, '1', '1', '1', 65535, 1
                )
                """
            ),
            {"batch_id": batch_id},
        )

    command.upgrade(config, "head")
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT submission_block, confirmation_state, confirmed_at,
                       confirmation_deadline_block, baseline_last_update_block,
                       period_blocks
                FROM weight_emission_batches
                """
            )
        ).one()
        emission_row = connection.execute(
            text(
                """
                SELECT batch_id, miner_hotkey, uid, weight_u16, emitted
                FROM weight_emission_rows
                """
            )
        ).one()

    assert tuple(row) == (None, None, None, None, None, None)
    assert tuple(emission_row) == (batch_id, "miner-a", 0, 65535, 1)

    prepared_values = {
        "schema_id": RISK_SCHEMA_ID,
        "emitted_at": "2026-08-09T00:01:00+00:00",
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO weight_emission_batches (
                    schema_id, round_id, emitted_at, block, min_allowed_weights,
                    max_weight_limit_text, metagraph_size, status,
                    submission_block, confirmation_state,
                    confirmation_deadline_block, baseline_last_update_block,
                    period_blocks,
                    chain_identity, netuid, validator_uid, validator_hotkey,
                    submission_mode, intent_hash
                ) VALUES (
                    :schema_id, NULL, :emitted_at, 101, 1, '1', 1, 'error',
                    100, 'prepared', 240, 90, 128,
                    'genesis-a', 1, 0, 'validator-hotkey', 'direct', :intent_hash
                )
                """
            ),
            {**prepared_values, "intent_hash": "a" * 64},
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO weight_emission_batches (
                        schema_id, round_id, emitted_at, block,
                        min_allowed_weights, max_weight_limit_text,
                        metagraph_size, status, submission_block,
                        confirmation_state, confirmation_deadline_block,
                        baseline_last_update_block, period_blocks, chain_identity,
                        netuid, validator_uid,
                        validator_hotkey, submission_mode, intent_hash
                    ) VALUES (
                        :schema_id, NULL, :emitted_at, 102, 1, '1', 1,
                        'error', 101, 'ambiguous', 241, 90, 128,
                        'genesis-a', 1, 0, 'validator-hotkey', 'direct', :intent_hash
                    )
                    """
                ),
                {**prepared_values, "intent_hash": "b" * 64},
            )

    with engine.begin() as connection:
        prepared_batch_id = connection.execute(
            text(
                "SELECT id FROM weight_emission_batches "
                "WHERE confirmation_state = 'prepared'"
            )
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO weight_emission_rows (
                    batch_id, miner_hotkey, uid, weight_processed_text,
                    weight_u16, emitted
                ) VALUES
                    (:batch_id, 'miner-b', 1, '1', 65535, 0),
                    (:batch_id, 'miner-c', 2, '0', NULL, 0)
                """
            ),
            {"batch_id": prepared_batch_id},
        )
        connection.execute(
            text(
                "UPDATE weight_emission_batches SET confirmation_state = 'confirmed', "
                "confirmed_at = '2026-08-09T00:02:00+00:00' WHERE id = :batch_id"
            ),
            {"batch_id": prepared_batch_id},
        )
        connection.execute(
            text(
                "UPDATE weight_emission_rows SET emitted = 1 "
                "WHERE batch_id = :batch_id AND weight_u16 IS NOT NULL"
            ),
            {"batch_id": prepared_batch_id},
        )

    command.downgrade(config, "0010_reveal_count")
    command.upgrade(config, "head")

    with engine.connect() as connection:
        preserved_rows = connection.execute(
            text(
                "SELECT batch_id, uid, weight_u16, emitted FROM weight_emission_rows "
                "ORDER BY batch_id, uid"
            )
        ).all()
        triggers = {
            str(row[0])
            for row in connection.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                    "AND name LIKE 'ck_weight_emission_%'"
                )
            )
        }
        backup_tables = connection.execute(
            text(
                "SELECT count(*) FROM sqlite_master WHERE type = 'table' "
                "AND name = '_weight_emission_rows_0011_backup'"
            )
        ).scalar_one()

    assert [tuple(row) for row in preserved_rows] == [
        (batch_id, 0, 65535, 1),
        (prepared_batch_id, 1, 65535, 1),
        (prepared_batch_id, 2, None, 0),
    ]
    assert triggers == {
        "ck_weight_emission_batch_insert_confirmed",
        "ck_weight_emission_batch_delete_confirmed",
        "ck_weight_emission_batch_update_confirmed",
        "ck_weight_emission_row_delete_confirmed",
        "ck_weight_emission_row_insert_confirmed",
        "ck_weight_emission_row_update_confirmed",
    }
    assert backup_tables == 0


def test_0011_upgrade_retries_after_sqlite_batch_rename_failure(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    database_path = tmp_path / "endure-weight-emission-retry.db"
    url = f"sqlite:///{database_path}"
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option(
        "script_location", str(repo_root / "endure/storage/migrations")
    )
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0010_reveal_count")
    engine = create_engine(url)
    with engine.begin() as connection:
        batch_id = connection.execute(
            text(
                """
                INSERT INTO weight_emission_batches (
                    schema_id, round_id, emitted_at, block, min_allowed_weights,
                    max_weight_limit_text, metagraph_size, status
                ) VALUES (
                    :schema_id, NULL, :emitted_at, 10, 1, '1', 1, 'submitted'
                )
                """
            ),
            {
                "schema_id": RISK_SCHEMA_ID,
                "emitted_at": "2026-08-09T00:00:00+00:00",
            },
        ).lastrowid
        assert batch_id is not None
        connection.execute(
            text(
                """
                INSERT INTO weight_emission_rows (
                    batch_id, miner_hotkey, uid, weight_processed_text,
                    weight_u16, emitted
                ) VALUES (:batch_id, 'miner-a', 0, '1', 65535, 1)
                """
            ),
            {"batch_id": batch_id},
        )

    rename_failed = False

    def fail_batch_rename_once(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        nonlocal rename_failed
        if rename_failed or not (
            statement.startswith("ALTER TABLE _alembic_tmp_weight_emission_batches")
            and "RENAME TO weight_emission_batches" in statement
        ):
            return
        rename_failed = True
        raise RuntimeError("forced 0011 batch rename failure")

    event.listen(Engine, "before_cursor_execute", fail_batch_rename_once)
    try:
        with pytest.raises(RuntimeError, match="forced 0011 batch rename failure"):
            command.upgrade(config, "head")
    finally:
        event.remove(Engine, "before_cursor_execute", fail_batch_rename_once)

    assert rename_failed is True
    command.upgrade(config, "head")

    with engine.connect() as connection:
        emission_rows = connection.execute(
            text("SELECT batch_id, uid, weight_u16, emitted FROM weight_emission_rows")
        ).all()
        transient_tables = connection.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
                "('_weight_emission_rows_0011_backup', "
                "'_alembic_tmp_weight_emission_batches')"
            )
        ).all()

    assert emission_rows == [(batch_id, 0, 65535, 1)]
    assert transient_tables == []


def test_0007_backfills_resolution_markers_before_scoring_replay(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    database_path = tmp_path / "endure-marker-backfill.db"
    url = f"sqlite:///{database_path}"
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option(
        "script_location", str(repo_root / "endure/storage/migrations")
    )
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0006_assessment_horizon_resolutions")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO rounds (
                    round_id, schema_id, state, universe_stale, degraded,
                    commit_open_at, commit_close_at, reveal_open_at,
                    reveal_close_at, t0_close_at, created_at, updated_at
                ) VALUES (
                    '2026-07-08', :schema_id, 'closed', 0, 0,
                    '2026-07-08T00:00:00+00:00', '2026-07-08T00:01:00+00:00',
                    '2026-07-08T00:01:00+00:00', '2026-07-08T00:02:00+00:00',
                    '2026-07-08T00:00:00+00:00', :now_iso, :now_iso
                )
                """
            ),
            {"schema_id": RISK_SCHEMA_ID, "now_iso": "2026-07-08T00:03:00+00:00"},
        )
        connection.execute(
            text(
                """
                INSERT INTO assessment_realized_targets (
                    round_id, schema_id, target_kind, target_id, horizon_kind,
                    horizon_value, output, value_text, status, provider_payload_hash,
                    fetched_at
                ) VALUES (
                    '2026-07-08', :schema_id, 'subnet_asset', '44', 'seconds',
                    2592000, :output, '1234', 'resolved', NULL,
                    '2026-07-08T00:04:00+00:00'
                )
                """
            ),
            {"schema_id": RISK_SCHEMA_ID, "output": RiskOutput.MAX_DRAWDOWN.value},
        )
        connection.execute(
            text(
                """
                INSERT INTO assessment_miner_score_state (
                    miner_hotkey, schema_id, target_kind, target_id, horizon_kind,
                    horizon_value, output, ema_text, resolved_rounds, updated_at
                ) VALUES (
                    'hk-a', :schema_id, 'subnet_asset', '44', 'seconds',
                    2592000, :output, '0.45', 1, '2026-07-08T00:05:00+00:00'
                )
                """
            ),
            {"schema_id": RISK_SCHEMA_ID, "output": RiskOutput.MAX_DRAWDOWN.value},
        )

    command.upgrade(config, "head")
    storage = Storage.from_url(url)
    coordinate = AssessmentCoordinate.subnet_asset(
        netuid=44, horizon_seconds=2_592_000, output=RiskOutput.MAX_DRAWDOWN.value
    )

    assert storage.has_assessment_resolution_marker(
        "2026-07-08", RISK_SCHEMA_ID, 2_592_000
    )
    storage.record_assessment_scoring_pass(
        "2026-07-08",
        RISK_SCHEMA_ID,
        horizon_value=2_592_000,
        realized_targets=(
            AssessmentRealizedTarget(
                coordinate=coordinate, value=Decimal(9999), status="resolved"
            ),
        ),
        output_scores=(),
        ema_updates=(
            AssessmentEmaState(
                miner_hotkey="hk-a",
                coordinate=coordinate,
                ema=Decimal(9999),
                resolved_rounds=2,
            ),
        ),
        score_history=(),
        now_iso="2026-07-08T00:06:00+00:00",
    )

    [ema] = storage.assessment_ema_states(RISK_SCHEMA_ID)
    assert ema.ema == Decimal("0.45")
    assert ema.resolved_rounds == 1


def test_0008_backfills_only_closed_accepted_bundles(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    database_path = tmp_path / "endure-consensus-snapshot-backfill.db"
    url = f"sqlite:///{database_path}"
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option(
        "script_location", str(repo_root / "endure/storage/migrations")
    )
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0007_backfill_assessment_resolution_markers")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO rounds (
                    round_id, schema_id, state, universe_stale, degraded,
                    commit_open_at, commit_close_at, reveal_open_at,
                    reveal_close_at, t0_close_at, created_at, updated_at
                ) VALUES (
                    :round_id, :schema_id, :state, 0, 0,
                    '2026-07-08T00:00:00+00:00', '2026-07-08T00:01:00+00:00',
                    '2026-07-08T00:01:00+00:00', '2026-07-08T00:02:00+00:00',
                    '2026-07-08T00:00:00+00:00', :now_iso, :now_iso
                )
                """
            ),
            [
                {
                    "round_id": "2026-07-08-closed",
                    "schema_id": RISK_SCHEMA_ID,
                    "state": "closed",
                    "now_iso": "2026-07-08T00:03:00+00:00",
                },
                {
                    "round_id": "2026-07-08-revealed",
                    "schema_id": RISK_SCHEMA_ID,
                    "state": "revealed",
                    "now_iso": "2026-07-08T00:03:00+00:00",
                },
                {
                    "round_id": "2026-07-08-partial",
                    "schema_id": RISK_SCHEMA_ID,
                    "state": "partially_scored",
                    "now_iso": "2026-07-08T00:03:00+00:00",
                },
                {
                    "round_id": "2026-07-08-open",
                    "schema_id": RISK_SCHEMA_ID,
                    "state": "open",
                    "now_iso": "2026-07-08T00:03:00+00:00",
                },
            ],
        )
        connection.execute(
            text(
                """
                INSERT INTO submissions (
                    round_id, schema_id, miner_hotkey, bundle_json, verdict
                ) VALUES (:round_id, :schema_id, :miner_hotkey, :bundle_json, :verdict)
                """
            ),
            [
                {
                    "round_id": "2026-07-08-closed",
                    "schema_id": RISK_SCHEMA_ID,
                    "miner_hotkey": "hk-accepted",
                    "bundle_json": '{"accepted":true}',
                    "verdict": "accepted",
                },
                {
                    "round_id": "2026-07-08-closed",
                    "schema_id": RISK_SCHEMA_ID,
                    "miner_hotkey": "hk-rejected",
                    "bundle_json": '{"rejected":true}',
                    "verdict": "rejected",
                },
                {
                    "round_id": "2026-07-08-revealed",
                    "schema_id": RISK_SCHEMA_ID,
                    "miner_hotkey": "hk-revealed",
                    "bundle_json": '{"revealed":true}',
                    "verdict": "accepted",
                },
                {
                    "round_id": "2026-07-08-partial",
                    "schema_id": RISK_SCHEMA_ID,
                    "miner_hotkey": "hk-partial",
                    "bundle_json": '{"partial":true}',
                    "verdict": "accepted",
                },
                {
                    "round_id": "2026-07-08-open",
                    "schema_id": RISK_SCHEMA_ID,
                    "miner_hotkey": "hk-open",
                    "bundle_json": '{"open":true}',
                    "verdict": "accepted",
                },
            ],
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        snapshot_rows = connection.execute(
            text(
                """
                SELECT round_id, miner_hotkey, bundle_json, snapshotted_at
                FROM consensus_bundle_snapshots
                ORDER BY round_id, miner_hotkey
                """
            )
        ).all()

    assert len(snapshot_rows) == 1
    round_id, miner_hotkey, bundle_json, snapshotted_at = snapshot_rows[0]
    assert (round_id, miner_hotkey, bundle_json) == (
        "2026-07-08-closed",
        "hk-accepted",
        '{"accepted":true}',
    )
    assert datetime.fromisoformat(snapshotted_at).tzinfo == UTC


def test_0008_upgrade_retries_after_backfill_failure(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    database_path = tmp_path / "endure-consensus-snapshot-retry.db"
    url = f"sqlite:///{database_path}"
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option(
        "script_location", str(repo_root / "endure/storage/migrations")
    )
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0007_backfill_assessment_resolution_markers")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO rounds (
                    round_id, schema_id, state, universe_stale, degraded,
                    commit_open_at, commit_close_at, reveal_open_at,
                    reveal_close_at, t0_close_at, created_at, updated_at
                ) VALUES (
                    '2026-07-08', :schema_id, 'closed', 0, 0,
                    '2026-07-08T00:00:00+00:00', '2026-07-08T00:01:00+00:00',
                    '2026-07-08T00:01:00+00:00', '2026-07-08T00:02:00+00:00',
                    '2026-07-08T00:00:00+00:00', :now_iso, :now_iso
                )
                """
            ),
            {"schema_id": RISK_SCHEMA_ID, "now_iso": "2026-07-08T00:03:00+00:00"},
        )
        connection.execute(
            text(
                """
                INSERT INTO submissions (
                    round_id, schema_id, miner_hotkey, bundle_json, verdict
                ) VALUES (
                    '2026-07-08', :schema_id, 'hk', :bundle_json, 'accepted'
                )
                """
            ),
            {"schema_id": RISK_SCHEMA_ID, "bundle_json": '{"accepted":true}'},
        )

    backfill_failed = False

    def fail_backfill_once(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        nonlocal backfill_failed
        if (
            backfill_failed
            or "INSERT OR IGNORE INTO consensus_bundle_snapshots" not in statement
        ):
            return
        backfill_failed = True
        raise RuntimeError("forced snapshot backfill failure")

    event.listen(Engine, "before_cursor_execute", fail_backfill_once)
    try:
        with pytest.raises(RuntimeError, match="forced snapshot backfill failure"):
            command.upgrade(config, "head")
    finally:
        event.remove(Engine, "before_cursor_execute", fail_backfill_once)

    assert backfill_failed is True
    command.upgrade(config, "head")

    with engine.connect() as connection:
        snapshot_rows = connection.execute(
            text(
                """
                SELECT miner_hotkey, bundle_json
                FROM consensus_bundle_snapshots
                """
            )
        ).all()

    assert snapshot_rows == [("hk", '{"accepted":true}')]


def test_head_drops_legacy_scoring_tables_and_keeps_every_shared_table(
    tmp_path: Path,
) -> None:
    # Given: a fresh database.
    config, engine = _alembic_config(tmp_path, "endure-fresh-head")

    # When: it is upgraded to head.
    command.upgrade(config, "head")

    # Then: the five dropped tables are absent and every shared table exists.
    tables = set(inspect(engine).get_table_names())
    assert tables.isdisjoint(DROPPED_TABLES)
    assert set(RETAINED_TABLES) <= tables


def test_0015_backfills_and_requires_publication_boundary(tmp_path: Path) -> None:
    config, engine = _alembic_config(tmp_path, "publication-embargo")
    command.upgrade(config, "0014_drop_kre_tables")
    reveal_close = "2026-08-27T00:00:00+00:00"
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO rounds (
                    round_id, schema_id, state, universe_stale, degraded,
                    commit_open_at, commit_close_at, reveal_open_at,
                    reveal_close_at, t0_close_at, created_at, updated_at
                ) VALUES (
                    '2026-08-26', :schema_id, 'revealed', 0, 0,
                    '2026-08-26T11:00:00+00:00',
                    '2026-08-26T19:30:00+00:00',
                    '2026-08-26T20:30:00+00:00', :reveal_close,
                    '2026-08-26T20:00:00+00:00', :reveal_close, :reveal_close
                )
                """
            ),
            {"schema_id": RISK_SCHEMA_ID, "reveal_close": reveal_close},
        )

    command.upgrade(config, "head")

    columns = {
        column["name"]: column for column in inspect(engine).get_columns("rounds")
    }
    assert columns["publication_available_at"]["nullable"] is False
    with engine.connect() as connection:
        publication_available_at = connection.execute(
            text(
                "SELECT publication_available_at FROM rounds "
                "WHERE round_id = '2026-08-26' AND schema_id = :schema_id"
            ),
            {"schema_id": RISK_SCHEMA_ID},
        ).scalar_one()
    assert publication_available_at == reveal_close

    command.downgrade(config, "0014_drop_kre_tables")
    assert "publication_available_at" not in {
        column["name"] for column in inspect(engine).get_columns("rounds")
    }


def test_0014_preserves_shared_rows_while_deleting_legacy_tables(
    tmp_path: Path,
) -> None:
    # Given: a database at the previous head carrying shared Alpha, weight
    # emission, and legacy scoring rows.
    config, engine = _alembic_config(tmp_path, "endure-drop-legacy")
    command.upgrade(config, PREVIOUS_HEAD)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO rounds (
                    round_id, schema_id, state, universe_stale, degraded,
                    commit_open_at, commit_close_at, reveal_open_at,
                    reveal_close_at, t0_close_at, created_at, updated_at
                ) VALUES (
                    '2026-08-20', :schema_id, 'closed', 0, 0,
                    '2026-08-20T00:00:00+00:00', '2026-08-20T00:01:00+00:00',
                    '2026-08-20T00:01:00+00:00', '2026-08-20T00:02:00+00:00',
                    '2026-08-20T00:00:00+00:00', :now_iso, :now_iso
                )
                """
            ),
            {"schema_id": RISK_SCHEMA_ID, "now_iso": "2026-08-20T00:03:00+00:00"},
        )
        connection.execute(
            text(
                """
                INSERT INTO universe_snapshots (
                    round_id, schema_id, tickers_json, source_hash, fetched_at
                ) VALUES ('2026-08-20', :schema_id, '["44"]', 'hash-a', :now_iso)
                """
            ),
            {"schema_id": RISK_SCHEMA_ID, "now_iso": "2026-08-20T00:00:00+00:00"},
        )
        connection.execute(
            text(
                """
                INSERT INTO submissions (
                    round_id, schema_id, miner_hotkey, bundle_json, verdict
                ) VALUES ('2026-08-20', :schema_id, 'hk-a', :bundle_json, 'accepted')
                """
            ),
            {"schema_id": RISK_SCHEMA_ID, "bundle_json": '{"accepted":true}'},
        )
        connection.execute(
            text(
                """
                INSERT INTO consensus_bundle_snapshots (
                    round_id, schema_id, miner_hotkey, bundle_json, snapshotted_at
                ) VALUES ('2026-08-20', :schema_id, 'hk-a', :bundle_json, :now_iso)
                """
            ),
            {
                "schema_id": RISK_SCHEMA_ID,
                "bundle_json": '{"accepted":true}',
                "now_iso": "2026-08-20T00:02:00+00:00",
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO assessment_consensus (
                    round_id, schema_id, target_kind, target_id, horizon_kind,
                    horizon_value, output, value_text, dispersion_text,
                    n_submitters, computed_at
                ) VALUES (
                    '2026-08-20', :schema_id, 'subnet_asset', '44', 'seconds',
                    3600, 'drawdown_bps', '1234.5', '10.25', 3, :now_iso
                )
                """
            ),
            {"schema_id": RISK_SCHEMA_ID, "now_iso": "2026-08-20T00:02:00+00:00"},
        )
        connection.execute(
            text(
                """
                INSERT INTO weight_emission_batches (
                    schema_id, emitted_at, metagraph_size, status, submission_block,
                    confirmation_state, confirmation_deadline_block,
                    baseline_last_update_block, period_blocks, chain_identity,
                    netuid, validator_uid, validator_hotkey, submission_mode,
                    intent_hash
                ) VALUES (
                    :schema_id, '2026-08-20T00:04:00+00:00', 2, 'submitted', 100,
                    'submitted', 240, 90, 128, 'genesis-a', 1, 0, 'validator-hotkey',
                    'cr4', '26:abc'
                )
                """
            ),
            {"schema_id": RISK_SCHEMA_ID},
        )
        connection.execute(
            text(
                """
                INSERT INTO realized_outcomes (
                    round_id, schema_id, ticker, horizon_trading_days, status
                ) VALUES ('2026-08-20', :schema_id, 'WAL', 5, 'resolved')
                """
            ),
            {"schema_id": RISK_SCHEMA_ID},
        )
        connection.execute(
            text(
                """
                INSERT INTO scoring_records (
                    round_id, schema_id, miner_hotkey, ticker,
                    horizon_trading_days, field, score_text, scored_at
                ) VALUES (
                    '2026-08-20', :schema_id, 'hk-a', 'WAL', 5,
                    'predicted_return_bps', '0.5', :now_iso
                )
                """
            ),
            {"schema_id": RISK_SCHEMA_ID, "now_iso": "2026-08-20T00:05:00+00:00"},
        )
        connection.execute(
            text(
                """
                INSERT INTO miner_score_state (
                    miner_hotkey, schema_id, horizon_trading_days, ema_text,
                    updated_at
                ) VALUES ('hk-a', :schema_id, 5, '0.5', :now_iso)
                """
            ),
            {"schema_id": RISK_SCHEMA_ID, "now_iso": "2026-08-20T00:05:00+00:00"},
        )
        connection.execute(
            text(
                """
                INSERT INTO score_history (
                    round_id, schema_id, miner_hotkey, horizon_trading_days,
                    round_score_text, ema_after_text, scored_at
                ) VALUES ('2026-08-20', :schema_id, 'hk-a', 5, '0.5', '0.5', :now_iso)
                """
            ),
            {"schema_id": RISK_SCHEMA_ID, "now_iso": "2026-08-20T00:05:00+00:00"},
        )
        connection.execute(
            text(
                """
                INSERT INTO consensus_aggregates (
                    round_id, schema_id, ticker, horizon_trading_days, field,
                    value_text, dispersion_text, n_submitters, computed_at
                ) VALUES (
                    '2026-08-20', :schema_id, 'WAL', 5, 'predicted_return_bps',
                    '-100', '10', 3, :now_iso
                )
                """
            ),
            {"schema_id": RISK_SCHEMA_ID, "now_iso": "2026-08-20T00:05:00+00:00"},
        )

    # When: the database is upgraded to head.
    command.upgrade(config, "head")

    # Then: the dropped tables are gone and every shared row survives unchanged.
    inspector = inspect(engine)
    assert set(inspector.get_table_names()).isdisjoint(DROPPED_TABLES)
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT round_id, schema_id, state FROM rounds")
        ).all() == [("2026-08-20", RISK_SCHEMA_ID, "closed")]
        assert connection.execute(
            text("SELECT tickers_json, source_hash FROM universe_snapshots")
        ).all() == [('["44"]', "hash-a")]
        assert connection.execute(
            text("SELECT miner_hotkey, bundle_json, verdict FROM submissions")
        ).all() == [("hk-a", '{"accepted":true}', "accepted")]
        assert connection.execute(
            text("SELECT miner_hotkey, bundle_json FROM consensus_bundle_snapshots")
        ).all() == [("hk-a", '{"accepted":true}')]
        assert connection.execute(
            text(
                "SELECT target_id, horizon_value, output, value_text, "
                "dispersion_text, n_submitters FROM assessment_consensus"
            )
        ).all() == [("44", 3600, "drawdown_bps", "1234.5", "10.25", 3)]
        assert connection.execute(
            text(
                "SELECT submission_block, confirmation_state, intent_hash "
                "FROM weight_emission_batches"
            )
        ).all() == [(100, "submitted", "26:abc")]


def test_0014_downgrade_recreates_empty_legacy_tables(tmp_path: Path) -> None:
    # Given: a database at head, where the dropped tables are absent.
    config, engine = _alembic_config(tmp_path, "endure-legacy-downgrade")
    command.upgrade(config, "head")

    # When: head is downgraded one revision.
    command.downgrade(config, PREVIOUS_HEAD)

    # Then: the five legacy tables and their indexes exist again, empty.
    inspector = inspect(engine)
    assert set(DROPPED_TABLES) <= set(inspector.get_table_names())
    recreated_indexes = {
        index["name"]
        for table in DROPPED_TABLES
        for index in inspector.get_indexes(table)
    }
    assert {
        "ix_miner_score_state_schema",
        "ix_realized_outcomes_schema_ticker",
        "ix_consensus_schema_ticker",
        "ix_score_history_schema_miner",
    } <= recreated_indexes
    with engine.connect() as connection:
        for name in DROPPED_TABLES:
            legacy = Table(name, MetaData(), autoload_with=engine)
            assert (
                connection.execute(
                    select(func.count()).select_from(legacy)
                ).scalar_one()
                == 0
            )

    # And: head is reachable again from the downgraded database.
    command.upgrade(config, "head")
    assert set(inspect(engine).get_table_names()).isdisjoint(DROPPED_TABLES)


def test_0014_upgrade_retries_after_partial_sqlite_drop(tmp_path: Path) -> None:
    # Given: a database at the previous head, and a 0014 run whose second
    # drop_table fails. SQLite migrations are non-transactional, so the drops
    # completed before the failure stay on disk while the version stays behind.
    config, engine = _alembic_config(tmp_path, "endure-drop-retry")
    command.upgrade(config, PREVIOUS_HEAD)
    real_drop_table = Operations.drop_table
    drop_calls = {"count": 0}

    def fail_second_drop(self: Operations, table_name: str, **kwargs: object) -> None:
        drop_calls["count"] += 1
        if drop_calls["count"] == 2:
            raise RuntimeError("forced drop_table failure")
        real_drop_table(self, table_name, **kwargs)

    # When: the upgrade is interrupted part-way through.
    with (
        pytest.MonkeyPatch.context() as patcher,
        pytest.raises(RuntimeError, match="forced drop_table failure"),
    ):
        patcher.setattr(Operations, "drop_table", fail_second_drop)
        command.upgrade(config, "head")

    # Then: the revision has not advanced, and the run really was partial.
    with engine.connect() as connection:
        version = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    assert version == PREVIOUS_HEAD
    assert drop_calls["count"] == 2
    assert not set(DROPPED_TABLES) <= set(inspect(engine).get_table_names())

    # And: retrying without the failure converges instead of crashing on the
    # indexes and tables the partial run already dropped.
    command.upgrade(config, "head")

    tables = set(inspect(engine).get_table_names())
    assert tables.isdisjoint(DROPPED_TABLES)
    assert set(RETAINED_TABLES) <= tables
