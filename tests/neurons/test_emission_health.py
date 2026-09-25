"""Consumer-facing emission expectedness without a first audit batch or health RPC."""

import threading
from collections.abc import Callable
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
from endure.scoring.emission_policy import EmissionBlocked, EmissionBlockReason
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
    result._durable_scores_loaded = True
    result._emission_block = None
    result._emission_block_since_block = None
    result._emission_block_seen_block = None
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


def test_a_due_attempt_is_judged_on_the_live_head_not_the_cached_metagraph(
    validator: Validator, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [10000.0]
    head = [1101]
    monkeypatch.setattr("neurons.validator.time.monotonic", lambda: now[0])
    monkeypatch.setattr(Validator, "block", property(lambda _self: head[0]))
    # The last plan, at live head 1050, found the chain due at 1100; the
    # metagraph was cached at the previous resync.
    validator._last_weights_attempt = 1000
    validator._emission_chain_due_block = 1100
    validator.metagraph.block = 1000

    assert validator.should_set_weights() is True

    assert validator._emission_reason == "ready"
    assert validator._emission_expected_since == 10000.0
    # A later /health poll still reads the cached metagraph block; it must not
    # restart the overdue clock of the attempt that is already due.
    now[0] = 10500.0
    health = validator.runtime_health()
    assert health["emission_reason"] == "ready"
    assert health["emission_expected_seconds"] == 500.0


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
    ("reason", "degraded_at_block"),
    [
        ("owner_hotkey_mismatch", 1000),
        ("owner_unregistered", 1000),
        ("owner_vote_chain_mismatch", 1000),
        ("owner_snapshot_inconsistent", 1200),
        ("chain_snapshot_inconsistent", 1200),
        ("validator_identity_invalid", 1200),
        ("score_state_unavailable", 1200),
    ],
)
def test_blocked_emission_abstains_and_degrades_by_severity(
    validator: Validator,
    monkeypatch: pytest.MonkeyPatch,
    reason: EmissionBlockReason,
    degraded_at_block: int,
) -> None:
    monkeypatch.setattr("neurons.validator.time.monotonic", lambda: 10000.0)
    validator.config.runtime.mode = "live"
    validator.config.subtensor.network = "finney"
    validator.config.subtensor.chain_endpoint = (
        "wss://entrypoint-finney.opentensor.ai:443"
    )
    validator.scores = [Decimal(0)]
    validator._mark_tick_progress()

    # Each paced attempt re-observes the same unsafe state.
    for block in (1000, 1199, 1200):
        validator._block_emission(EmissionBlocked(reason, "unsafe chain state"), block)
        validator.metagraph.block = block
        response = _client(validator).get("/health")
        runtime = response.json()["runtime"]
        assert runtime["emission_mode"] == "abstain"
        assert runtime["emission_reason"] == reason
        assert runtime["emission_blocked_reason"] == reason
        assert runtime["emission_expected"] is False
        assert response.status_code == (503 if block >= degraded_at_block else 200)


def test_a_transient_block_that_is_not_reobserved_never_pages(
    validator: Validator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("neurons.validator.time.monotonic", lambda: 10000.0)
    validator.scores = [Decimal(0)]
    validator._mark_tick_progress()
    validator._block_emission(
        EmissionBlocked("chain_snapshot_inconsistent", "stale snapshot"), 1000
    )

    # The condition cleared on chain; no later attempt has observed it again.
    validator.metagraph.block = 1300
    response = _client(validator).get("/health")

    assert response.status_code == 200
    assert response.json()["runtime"]["emission_blocked_reason"] == (
        "chain_snapshot_inconsistent"
    )


def test_one_clock_covers_a_blocked_streak_across_flapping_reasons(
    validator: Validator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("neurons.validator.time.monotonic", lambda: 10000.0)
    validator.scores = [Decimal(0)]
    validator._mark_tick_progress()
    reasons: tuple[EmissionBlockReason, ...] = (
        "owner_snapshot_inconsistent",
        "chain_snapshot_inconsistent",
        "owner_snapshot_inconsistent",
    )

    statuses = []
    for block, reason in zip((1000, 1100, 1200), reasons, strict=True):
        validator._block_emission(EmissionBlocked(reason, "flapping"), block)
        validator.metagraph.block = block
        statuses.append(_client(validator).get("/health").status_code)

    # Nothing was emitted for two epochs: the streak pages although no single
    # reason persisted that long.
    assert statuses == [200, 200, 503]
    # Resolution ends the streak; a new block starts a fresh clock.
    validator._clear_emission_block()
    validator._block_emission(
        EmissionBlocked("chain_snapshot_inconsistent", "again"), 1300
    )
    validator.metagraph.block = 1300
    assert _client(validator).get("/health").status_code == 200


def test_a_score_read_failure_does_not_restart_the_interrupted_fault_clock(
    validator: Validator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("neurons.validator.time.monotonic", lambda: 10000.0)
    validator.scores = [Decimal(0)]
    validator._mark_tick_progress()
    runtime = MagicMock()
    runtime.round_program.weights.return_value = {}
    runtime.round_program.blended_scores.return_value = {}
    validator._vertical_runtime = runtime
    validator._block_emission(
        EmissionBlocked("chain_snapshot_inconsistent", "stale"), 1000
    )
    validator._block_emission(
        EmissionBlocked("score_state_unavailable", "database is locked"), 1100
    )

    # The score read recovers; the snapshot fault is still there at 1200.
    assert validator._refresh_scores_from_durable_state()
    assert validator._emission_block == "chain_snapshot_inconsistent"
    validator._block_emission(
        EmissionBlocked("chain_snapshot_inconsistent", "stale"), 1200
    )
    validator.metagraph.block = 1200

    assert _client(validator).get("/health").status_code == 503


def test_a_score_read_failure_keeps_an_immediate_fault_paging(
    validator: Validator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("neurons.validator.time.monotonic", lambda: 10000.0)
    validator.scores = [Decimal(0)]
    validator._mark_tick_progress()
    validator._block_emission(
        EmissionBlocked("owner_hotkey_mismatch", "owner rotated"), 1000
    )
    assert _client(validator).get("/health").status_code == 503

    # A score-read failure one block later parks the owner fault underneath.
    validator._block_emission(
        EmissionBlocked("score_state_unavailable", "database is locked"), 1001
    )

    assert validator._emission_block == "score_state_unavailable"
    assert _client(validator).get("/health").status_code == 503


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


def test_emission_transitions_log_only_after_releasing_the_health_lock(
    validator: Validator, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stalled log sink must not hold off /health: another thread must be
    # able to take the emission-state lock while a transition is logged.
    lock_free_while_logging: list[bool] = []

    def probe(_message: str) -> None:
        acquired: list[bool] = []

        def other_thread() -> None:
            got = Validator._emission_state_lock.acquire(blocking=False)
            if got:
                Validator._emission_state_lock.release()
            acquired.append(got)

        worker = threading.Thread(target=other_thread)
        worker.start()
        worker.join()
        lock_free_while_logging.extend(acquired)

    monkeypatch.setattr("neurons.validator.bt.logging.info", probe)
    validator._block_emission(
        EmissionBlocked("chain_snapshot_inconsistent", "stale"), 1000
    )

    assert lock_free_while_logging == [True]


def _held_by_another_thread_or_caller() -> bool:
    """True when some thread holds the emission lock (probed from outside)."""
    acquired: list[bool] = []

    def probe() -> None:
        got = Validator._emission_state_lock.acquire(blocking=False)
        if got:
            Validator._emission_state_lock.release()
        acquired.append(got)

    worker = threading.Thread(target=probe)
    worker.start()
    worker.join()
    return not acquired[0]


def _check_storage_calls(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """Fail any durable read or write made while a thread holds the lock."""
    calls: list[str] = []

    def checked(name: str, target: Callable[..., object]) -> Callable[..., object]:
        def call(*args: object, **kwargs: object) -> object:
            assert not _held_by_another_thread_or_caller(), (
                f"storage.{name} called while the emission lock is held"
            )
            calls.append(name)
            return target(*args, **kwargs)

        return call

    for name in dir(Storage):
        target = getattr(storage, name)
        if not name.startswith("_") and callable(target):
            monkeypatch.setattr(storage, name, checked(name, target))
    return calls


def test_emission_checks_never_touch_storage_under_the_lock_or_every_pass(
    validator: Validator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("neurons.validator.time.monotonic", lambda: 10000.0)
    validator.config.runtime.mode = "live"
    calls = _check_storage_calls(validator._storage, monkeypatch)
    validator._storage.record_weight_emission_startup_fence(
        schema_id=RISK_SCHEMA_ID,
        protocol_version_key=CURRENT_VERSION_KEY,
        fence_block=900,
    )
    validator._mark_tick_progress()

    validator.should_set_weights()  # first pass loads fence and open state
    validator.runtime_health()
    assert calls, "the instrumented storage saw the first pass"
    calls.clear()
    for _ in range(5):
        validator.should_set_weights()

    # Steady-state loop passes answer from memory: no durable query at all.
    assert calls == []
    assert validator._emission_reason == "ready"


def test_a_stalled_health_read_never_delays_the_run_loop_or_live(
    validator: Validator, monkeypatch: pytest.MonkeyPatch
) -> None:
    validator._mark_tick_progress()
    validator.should_set_weights()  # load in-memory emission state
    stalled = threading.Event()
    release = threading.Event()
    summary = validator._storage.weight_emission_confirmation_health

    def stall(*, schema_id: str, current_block: int | None) -> object:
        stalled.set()
        release.wait(30)
        return summary(schema_id=schema_id, current_block=current_block)

    monkeypatch.setattr(
        validator._storage, "weight_emission_confirmation_health", stall
    )
    health_status: list[int] = []
    poll = threading.Thread(
        target=lambda: health_status.append(
            _client(validator).get("/health").status_code
        )
    )
    poll.start()
    try:
        assert stalled.wait(10), "the /health read never reached storage"
        # The run loop's emission check must not queue behind the stalled read.
        loop_done = threading.Event()
        loop = threading.Thread(
            target=lambda: (validator.should_set_weights(), loop_done.set())
        )
        loop.start()
        assert loop_done.wait(5), "should_set_weights waited behind /health"
        assert _client(validator).get("/live").status_code == 200
    finally:
        release.set()
        poll.join(30)
    assert health_status == [200]
