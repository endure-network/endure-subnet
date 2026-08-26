"""Restart-path tests for SQLite-only emission score authority."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import bittensor as bt
import numpy as np

from endure.assessment.coordinates import AssessmentCoordinate, AssessmentEmaState
from endure.assessment.schemas.subnet_alpha_risk import (
    HORIZON_5D_SECONDS,
    RISK_SCHEMA_ID,
    RiskOutput,
)
from endure.storage.repository import Storage

NOW = "2026-08-12T12:00:00+00:00"
SCORING_ROUND = "2026-08-12"


def _configure_risk_boot(config: bt.Config) -> None:
    config.endure.active_schema = RISK_SCHEMA_ID
    config.neuron.axon_off = True
    config.neuron.disable_set_weights = True


def _state_path(config: bt.Config) -> Path:
    return (
        Path(config.logging.logging_dir)
        / config.wallet.name
        / config.wallet.hotkey
        / f"netuid{config.netuid}"
        / config.neuron.name
        / "state.npz"
    )


def _migrated_storage(config: bt.Config) -> Storage:
    from neurons.validator import _run_migrations

    _run_migrations(config.endure.database_url)
    return Storage.from_url(config.endure.database_url)


def _ema(hotkey: str, value: str) -> AssessmentEmaState:
    return AssessmentEmaState(
        miner_hotkey=hotkey,
        coordinate=AssessmentCoordinate.subnet_asset(
            netuid=44,
            horizon_seconds=HORIZON_5D_SECONDS,
            output=RiskOutput.MAX_DRAWDOWN.value,
        ),
        ema=Decimal(value),
        resolved_rounds=1,
    )


def test_startup_reconstructs_scores_from_sqlite_after_npz_gap(
    mock_validator_config: bt.Config,
) -> None:
    from neurons.validator import Validator

    # Given: unequal raw EMA blends committed to SQLite with no NPZ checkpoint.
    _configure_risk_boot(mock_validator_config)
    state_path = _state_path(mock_validator_config)
    storage = _migrated_storage(mock_validator_config)
    storage.upsert_assessment_ema(
        RISK_SCHEMA_ID, _ema("miner-hotkey-1", "0.3"), now_iso=NOW
    )
    storage.upsert_assessment_ema(
        RISK_SCHEMA_ID, _ema("miner-hotkey-2", "0.7"), now_iso=NOW
    )
    storage._engine.dispose()
    assert not state_path.exists()

    # When: the validator boots but has not run its first tick.
    validator = Validator(config=mock_validator_config)
    raw_blends = validator._service.blended_snapshot()
    program = validator._service._round_program
    assert program is not None
    weights = program.weights()
    expected_raw = {
        "miner-hotkey-1": Decimal("0.3"),
        "miner-hotkey-2": Decimal("0.7"),
    }
    expected_weights = {
        "miner-hotkey-1": Decimal("0.07297297297297297297297297297"),
        "miner-hotkey-2": Decimal("0.9270270270270270270270270270"),
    }

    # Then: startup installs cubic-sharpened weights, never linear blends.
    assert validator._last_tick_ok is None
    assert raw_blends == expected_raw
    assert weights == expected_weights
    expected_scores = [weights.get(hotkey, Decimal(0)) for hotkey in validator.hotkeys]
    raw_scores = [raw_blends.get(hotkey, Decimal(0)) for hotkey in validator.hotkeys]
    assert validator.scores == expected_scores
    assert any(score > Decimal(0) for score in validator.scores)
    assert validator.scores != raw_scores


def test_fresh_chain_abstains_when_sqlite_and_npz_have_no_scores(
    mock_validator_config: bt.Config,
) -> None:
    from neurons.validator import Validator

    # Given: a fresh SQLite database and no lifecycle checkpoint.
    _configure_risk_boot(mock_validator_config)
    assert not _state_path(mock_validator_config).exists()

    # When: the validator boots and attempts its first weight emission.
    validator = Validator(config=mock_validator_config)
    with patch.object(
        validator.subtensor,
        "set_weights",
        wraps=validator.subtensor.set_weights,
    ) as set_weights:
        validator.set_weights()

    # Then: zero scores remain authoritative and the mock chain is untouched.
    assert validator.scores == [Decimal(0)] * len(validator.hotkeys)
    set_weights.assert_not_called()


def test_startup_ignores_stale_legacy_npz_scores_end_to_end(
    mock_validator_config: bt.Config,
) -> None:
    from neurons.validator import Validator

    # Given: SQLite truth conflicts with an obviously invalid legacy score checkpoint.
    _configure_risk_boot(mock_validator_config)
    storage = _migrated_storage(mock_validator_config)
    storage.upsert_assessment_ema(
        RISK_SCHEMA_ID, _ema("miner-hotkey-1", "0.55"), now_iso=NOW
    )
    storage._engine.dispose()
    state_path = _state_path(mock_validator_config)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        state_path,
        step=13,
        scores=np.array(["9"]),
        hotkeys=np.array(["legacy-hotkey"]),
    )

    # When: the full validator initialization path loads and replaces startup state.
    validator = Validator(config=mock_validator_config)
    program = validator._service._round_program
    assert program is not None
    weights = program.weights()

    # Then: only the DB-derived vector survives reconstruction.
    assert validator.step == 13
    assert validator.scores == [
        weights.get(hotkey, Decimal(0)) for hotkey in validator.hotkeys
    ]
    assert Decimal("9") not in validator.scores


def test_restart_recovers_committed_scores_after_uncheckpointed_crash(
    mock_validator_config: bt.Config,
) -> None:
    from neurons.validator import Validator

    # Given: a scoring pass commits EMA updates, then the process dies before save_state().
    _configure_risk_boot(mock_validator_config)
    storage = _migrated_storage(mock_validator_config)
    storage.record_assessment_scoring_pass(
        SCORING_ROUND,
        RISK_SCHEMA_ID,
        horizon_value=HORIZON_5D_SECONDS,
        realized_targets=[],
        output_scores=[],
        ema_updates=[
            _ema("miner-hotkey-1", "0.4"),
            _ema("miner-hotkey-2", "0.8"),
        ],
        score_history=[],
        complete=False,
        now_iso=NOW,
    )
    storage._engine.dispose()
    assert not _state_path(mock_validator_config).exists()

    # When: a fresh validator process boots against the same SQLite file.
    restarted = Validator(config=mock_validator_config)
    program = restarted._service._round_program
    assert program is not None
    weights = program.weights()

    # Then: the emission-ready vector is reconstructed from the committed transaction.
    assert restarted._service.blended_snapshot() == {
        "miner-hotkey-1": Decimal("0.4"),
        "miner-hotkey-2": Decimal("0.8"),
    }
    assert restarted.scores == [
        weights.get(hotkey, Decimal(0)) for hotkey in restarted.hotkeys
    ]
    assert any(score > Decimal(0) for score in restarted.scores)
