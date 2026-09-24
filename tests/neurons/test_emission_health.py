"""Consumer-facing emission expectedness without a first audit batch or health RPC."""

from decimal import Decimal
from unittest.mock import MagicMock

import bittensor as bt
import pytest
from fastapi.testclient import TestClient

from endure.api.app import build_app
from endure.assessment.schemas.subnet_alpha_risk import RISK_SCHEMA_ID
from endure.protocol.version_contract import CURRENT_VERSION_KEY
from endure.protocol.weight_intent import (
    WeightIntentPayload,
    canonical_weight_intent_hash,
)
from endure.scoring.emission_policy import OwnerVoteBlockReason
from endure.storage.repository import Storage
from neurons.validator import Validator


@pytest.fixture
def validator(mock_validator_config: bt.Config, storage: Storage) -> Validator:
    result = Validator.__new__(Validator)
    result.config = mock_validator_config
    result.config.neuron.disable_set_weights = False
    result.config.neuron.epoch_length = 100
    result.config.endure.health_startup_grace_seconds = 300
    result.config.endure.health_tick_max_duration_seconds = 1800
    result._storage = storage
    result._schema_id = RISK_SCHEMA_ID
    result.scores = [Decimal(1)]
    result.uid = 0
    result.step = 1
    result.wallet = MagicMock()
    result.wallet.hotkey.ss58_address = "validator"
    result.metagraph = MagicMock()
    result.metagraph.block = 1000
    result.metagraph.hotkeys = ["validator"]
    result.metagraph.validator_permit = [True]
    result._last_weights_attempt = 800
    result._started_monotonic = 0.0
    result._process_started_at = "2026-09-24T00:00:00+00:00"
    result._last_tick_monotonic = 0.0
    result._last_tick_ok = None
    result._last_tick_error = None
    result._tick_failures = 0
    result._last_set_weights_ok = None
    result._consecutive_set_weights_failures = 0
    result._service = MagicMock(
        consecutive_universe_failures=0,
        last_universe_error=None,
        consecutive_resolution_failures=0,
        last_resolution_error=None,
        consecutive_empty_scored_rounds=0,
        last_empty_scored_round=None,
    )
    result.rpc_gate = MagicMock()
    result.rpc_gate.ready.return_value = True
    result.rpc_gate.snapshot.return_value = MagicMock(
        adaptive_rate=1.0,
        degraded=False,
        abandoned_generations=0,
        rate_limited_total=0,
        deferred_total=0,
    )
    result.thread = MagicMock()
    result.thread.is_alive.return_value = True
    result.subtensor = MagicMock()
    result.subtensor.get_current_block.return_value = 1000
    result.gated_subtensor = MagicMock()
    return result


def _client(validator: Validator) -> TestClient:
    return TestClient(
        build_app(
            storage=validator._storage,
            schema_id=RISK_SCHEMA_ID,
            publisher="assessment",
            runtime_health=validator.runtime_health,
        )
    )


def test_first_due_submission_expires_without_any_audit_or_health_poll(
    validator: Validator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1000.0]
    monkeypatch.setattr("neurons.validator.time.monotonic", lambda: now[0])
    assert validator.should_set_weights() is True
    # Another due scheduler pass must not renew the first-submission deadline.
    now[0] = 2801.0
    assert validator.should_set_weights() is True
    validator._mark_tick_progress()
    validator.subtensor.reset_mock()
    response = _client(validator).get("/health")
    assert response.status_code == 503
    runtime = response.json()["runtime"]
    assert runtime["emission_mode"] == "scored"
    assert runtime["emission_reason"] == "submission_overdue"
    assert runtime["emission_expected_seconds"] == 1801.0
    assert runtime["emission_deadline_in_seconds"] == -1.0
    assert runtime["open_weight_submissions"] == 0
    assert runtime["last_confirmed_weights_at"] is None
    assert validator.subtensor.mock_calls == []


@pytest.mark.parametrize(
    "wait",
    [
        "disabled",
        "no_positive_scores",
        "no_validator_permit",
        "epoch_pacing",
        "chain_rate_limit",
        "startup_fence",
    ],
)
def test_intentional_wait_does_not_create_missing_submission_fault(
    validator: Validator,
    monkeypatch: pytest.MonkeyPatch,
    wait: str,
) -> None:
    monkeypatch.setattr("neurons.validator.time.monotonic", lambda: 10000.0)
    if wait == "disabled":
        validator.config.neuron.disable_set_weights = True
    elif wait == "no_positive_scores":
        validator.scores = [Decimal(0)]
    elif wait == "no_validator_permit":
        validator.metagraph.validator_permit = [False]
    elif wait == "epoch_pacing":
        validator._last_weights_attempt = 950
    elif wait == "chain_rate_limit":
        validator._emission_chain_due_block = 1100
    else:
        validator.config.runtime.mode = "live"
        validator._storage.record_weight_emission_startup_fence(
            schema_id=RISK_SCHEMA_ID,
            protocol_version_key=CURRENT_VERSION_KEY,
            fence_block=1241,
        )
    validator._mark_tick_progress()
    response = _client(validator).get("/health")
    assert response.status_code == 200
    runtime = response.json()["runtime"]
    assert runtime["emission_reason"] == wait
    assert runtime["emission_expected"] is False
    assert runtime["emission_submission_overdue"] is False


def test_startup_fence_and_following_epoch_precede_missing_progress_deadline(
    validator: Validator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [10000.0]
    monkeypatch.setattr("neurons.validator.time.monotonic", lambda: now[0])
    validator.config.runtime.mode = "live"
    validator._storage.record_weight_emission_startup_fence(
        schema_id=RISK_SCHEMA_ID,
        protocol_version_key=CURRENT_VERSION_KEY,
        fence_block=1241,
    )
    validator._last_weights_attempt = 1202
    validator.metagraph.block = 1242
    health = validator.runtime_health()
    assert health["emission_reason"] == "epoch_pacing"
    assert health["emission_next_eligible_block"] == 1303
    assert health["emission_expected"] is False
    now[0] = 11000.0
    validator.metagraph.block = 1303
    health = validator.runtime_health()
    assert health["emission_expected"] is True
    assert health["emission_deadline_in_seconds"] == 1800.0
    assert health["emission_submission_overdue"] is False


@pytest.mark.parametrize(
    ("network", "endpoint"),
    [
        ("finney", "wss://entrypoint-finney.opentensor.ai:443"),
        ("test", "wss://test.finney.opentensor.ai:443"),
    ],
)
def test_modes_follow_scores_owner_vote_and_explicit_off(
    validator: Validator, network: str, endpoint: str
) -> None:
    validator.config.runtime.mode = "live"
    validator.config.netuid = 30
    validator.config.subtensor.network = network
    validator.config.subtensor.chain_endpoint = endpoint
    validator.scores = [Decimal(0)] * 3
    assert validator.runtime_health()["emission_mode"] == "owner_vote"
    validator.scores[1] = Decimal("0.5")
    assert validator.runtime_health()["emission_mode"] == "scored"
    # Archival back to all-zero scores returns to the owner vote; no latch.
    validator.scores[1] = Decimal(0)
    assert validator.runtime_health()["emission_mode"] == "owner_vote"
    validator.config.neuron.disable_set_weights = True
    validator.set_weights()
    assert validator.runtime_health()["emission_mode"] == "disabled"
    assert validator.subtensor.mock_calls == []


@pytest.mark.parametrize(
    "reason",
    [
        "owner_hotkey_mismatch",
        "owner_unregistered",
        "owner_snapshot_inconsistent",
        "owner_vote_chain_mismatch",
    ],
)
def test_blocked_owner_vote_reports_a_distinct_abstain_reason(
    validator: Validator,
    monkeypatch: pytest.MonkeyPatch,
    reason: OwnerVoteBlockReason,
) -> None:
    monkeypatch.setattr("neurons.validator.time.monotonic", lambda: 10000.0)
    validator.config.runtime.mode = "live"
    validator.config.subtensor.network = "finney"
    validator.config.subtensor.chain_endpoint = (
        "wss://entrypoint-finney.opentensor.ai:443"
    )
    validator.scores = [Decimal(0)]
    validator._owner_vote_block_reason = reason
    validator._mark_tick_progress()

    response = _client(validator).get("/health")

    assert response.status_code == 200
    runtime = response.json()["runtime"]
    assert runtime["emission_mode"] == "abstain"
    assert runtime["emission_reason"] == reason
    assert runtime["emission_expected"] is False


def test_confirmation_deadline_still_degrades_while_emission_is_disabled(
    validator: Validator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("neurons.validator.time.monotonic", lambda: 1000.0)
    validator._storage.record_weight_emission(
        schema_id=RISK_SCHEMA_ID,
        round_id=None,
        emitted_at_iso="2026-09-24T00:00:00+00:00",
        block=900,
        min_allowed_weights=1,
        max_weight_limit=Decimal(1),
        metagraph_size=1,
        status="submitted",
        rows=[],
        submission_block=900,
        confirmation_state="submitted",
        baseline_last_update_block=800,
        period_blocks=100,
        confirmation_deadline_block=1040,
        chain_identity="local-chain",
        netuid=1,
        validator_uid=0,
        validator_hotkey="validator",
        submission_mode="direct",
        protocol_version_key=CURRENT_VERSION_KEY,
        intent_hash=canonical_weight_intent_hash(
            WeightIntentPayload(
                protocol_version_key=CURRENT_VERSION_KEY,
                chain_identity="local-chain",
                netuid=1,
                validator_uid=0,
                validator_hotkey="validator",
                targets=(),
            )
        ),
    )
    validator._mark_tick_progress()
    client = _client(validator)
    waiting = client.get("/health")
    assert waiting.status_code == 200
    assert waiting.json()["runtime"]["emission_reason"] == "confirmation_pending"
    assert waiting.json()["runtime"]["last_confirmed_weights_at"] is None
    validator.config.neuron.disable_set_weights = True
    validator.metagraph.block = 1041
    overdue = client.get("/health")
    assert overdue.status_code == 503
    runtime = overdue.json()["runtime"]
    assert runtime["emission_mode"] == "disabled"
    assert runtime["emission_submission_overdue"] is False
    assert runtime["emission_confirmation_deadline_block"] == 1040
    assert runtime["weight_emission_degraded"] is True
