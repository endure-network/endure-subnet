"""One durable emitter transitions from SN30 bootstrap to earned weights."""

from __future__ import annotations

import copy
import sqlite3
from contextlib import closing
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import bittensor as bt
import numpy as np
import pytest
from bittensor.core.types import ExtrinsicResponse

from endure.assessment.coordinates import AssessmentEmaState, AssessmentScoreHistoryRow
from endure.assessment.registry import UniverseSnapshot
from endure.assessment.schemas.subnet_alpha_risk import (
    HORIZON_5D_SECONDS,
    RISK_HORIZONS,
    RISK_SCHEMA_ID,
    RiskOutput,
    RiskSubmissionBundle,
)
from endure.base.validator import WEIGHT_EMISSION_FINALITY_MARGIN_BLOCKS
from endure.protocol.consensus_policy import (
    MAINNET_GENESIS_HASH,
    SN30_BOOTSTRAP_HOTKEY,
    SN30_BOOTSTRAP_UID,
)
from endure.protocol.round_engine import DEFAULT_OFFSETS, compute_fixed_utc_windows
from endure.protocol.schedulers import FixedUtcScheduler
from endure.protocol.version_contract import CURRENT_VERSION_KEY
from endure.protocol.vertical import AssessmentRoundProgram, VerticalRuntime
from endure.scoring.emission_policy import BootstrapPolicyError
from endure.scoring.market_data import recorded_mainnet_fixture_provider
from endure.scoring.risk.orchestrator import RiskScoringOrchestrator, risk_coordinate
from endure.scoring.weights import ema_update
from endure.storage.repository import Storage, WeightEmissionChainSnapshot
from neurons.validator import Validator

NOW = "2026-09-24T20:00:00+00:00"


class ReplayChain:
    """Stateful SDK boundary; no network, wallet signing, or chain mutation."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self.block = 1_000
        self.hotkeys = [f"hotkey-{uid}" for uid in range(SN30_BOOTSTRAP_UID + 1)]
        self.hotkeys[SN30_BOOTSTRAP_UID] = SN30_BOOTSTRAP_HOTKEY
        self.owner_hotkey = SN30_BOOTSTRAP_HOTKEY
        self.permits = [True] + [False] * SN30_BOOTSTRAP_UID
        self.last_updates = [0] * len(self.hotkeys)
        self.weights_rate_limit = 180
        self.minimum = 1
        self.maximum = Decimal("1")
        self.submissions: list[tuple[tuple[int, ...], tuple[int, ...], int]] = []
        self.chain_weights: tuple[tuple[int, int], ...] = ()
        self.fail_after_submit = False
        self.crash_before_send = False

    def get_current_block(self) -> int:
        return self.block

    def get_block_hash(self, block: int) -> str:
        assert block == 0
        return MAINNET_GENESIS_HASH

    def commit_reveal_enabled(self, *, netuid: int) -> bool:
        assert netuid == 30
        return False

    def min_allowed_weights(self, *, netuid: int) -> int:
        return self.minimum

    def max_weight_limit(self, *, netuid: int) -> Decimal:
        return self.maximum

    def get_metagraph_info(self, netuid: int, *, block: int) -> SimpleNamespace:
        assert netuid == 30 and block == self.block
        return SimpleNamespace(
            block=block,
            hotkeys=list(self.hotkeys),
            owner_hotkey=self.owner_hotkey,
            validator_permit=list(self.permits),
            last_update=list(self.last_updates),
            weights_rate_limit=self.weights_rate_limit,
        )

    def set_weights(
        self,
        *,
        uids: list[int],
        weights: list[int],
        version_key: int,
        **_kwargs: object,
    ) -> ExtrinsicResponse:
        # The real storage intent must exist before a possible submission.
        assert self.storage.has_open_weight_emission_confirmation(
            schema_id=RISK_SCHEMA_ID
        )
        if self.crash_before_send:
            raise SystemExit("process lost after preparation, before transport")
        self.submissions.append((tuple(uids), tuple(weights), version_key))
        self.block += 1  # Inclusion is strictly after the preparation boundary.
        self.chain_weights = tuple(zip(uids, weights, strict=True))
        self.last_updates[0] = self.block
        if self.fail_after_submit:
            raise OSError("connection lost after submission")
        return ExtrinsicResponse(True, "submitted")

    def confirm(self) -> None:
        self.block = self.last_updates[0] + WEIGHT_EMISSION_FINALITY_MARGIN_BLOCKS + 1
        result = self.storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=WeightEmissionChainSnapshot(
                chain_identity=MAINNET_GENESIS_HASH,
                netuid=30,
                validator_uid=0,
                validator_hotkey=self.hotkeys[0],
                block=self.block,
                last_update_block=self.last_updates[0],
                weights=self.chain_weights,
                commitments=(),
                reveals=(),
                hotkeys=tuple(enumerate(self.hotkeys)),
            ),
            finality_margin_blocks=WEIGHT_EMISSION_FINALITY_MARGIN_BLOCKS,
            confirmed_at_iso=NOW,
        )
        assert result.confirmed == 1


class ReplayValidator(Validator):
    @property
    def block(self) -> int:
        # A virtual chain clock must not wait for the live adapter's TTL cache.
        return self.subtensor.get_current_block()


def replay_validator(
    storage: Storage, config: bt.Config, chain: ReplayChain
) -> ReplayValidator:
    validator = ReplayValidator.__new__(ReplayValidator)
    validator.config = copy.deepcopy(config)
    validator.config.runtime.mode = "live"
    validator.config.subtensor.network = "finney"
    validator.config.netuid = 30
    validator.config.endure.active_schema = RISK_SCHEMA_ID
    validator.config.endure.serving_stage = "mainnet"
    validator.config.neuron.disable_set_weights = False
    validator.config.neuron.axon_off = False
    validator._storage = storage
    validator._schema_id = RISK_SCHEMA_ID
    validator._positive_score_history_id = 0
    validator._has_positive_score_history = False
    validator._bootstrap_chain_snapshot = None
    validator._weight_emission_startup_fence_block = None
    validator._consecutive_provider_throttles = 0
    validator._consecutive_set_weights_failures = 0
    validator.metagraph = SimpleNamespace(
        hotkeys=list(chain.hotkeys),
        uids=np.arange(len(chain.hotkeys)),
        n=len(chain.hotkeys),
        last_update=chain.last_updates,
    )
    validator.uid = 0
    validator.wallet = SimpleNamespace(
        hotkey=SimpleNamespace(ss58_address=chain.hotkeys[0])
    )
    validator.subtensor = chain
    validator.gated_subtensor = chain
    orchestrator = RiskScoringOrchestrator(
        storage=storage,
        price_provider=recorded_mainnet_fixture_provider(),
        half_life_rounds=5,
        reveal_close_block=lambda _close: 100,
        registered_hotkeys=lambda: validator.metagraph.hotkeys,
    )
    program = AssessmentRoundProgram(
        storage=storage,
        schema_id=RISK_SCHEMA_ID,
        bundle_model=RiskSubmissionBundle,
        orchestrator=orchestrator,
        horizons=RISK_HORIZONS,
        due_seconds_by_horizon={horizon: horizon for horizon in RISK_HORIZONS},
    )
    validator._vertical_runtime = VerticalRuntime(
        round_program=program,
        publisher="risk",
        scheduler=FixedUtcScheduler(fetch_delay_seconds=0),
    )
    validator._reconstruct_scores()
    return validator


def advance_past_startup_fence(validator: ReplayValidator, chain: ReplayChain) -> None:
    validator.set_weights()
    assert chain.submissions == []
    fence = chain.storage.weight_emission_startup_fence(
        schema_id=RISK_SCHEMA_ID, protocol_version_key=CURRENT_VERSION_KEY
    )
    assert fence is not None
    chain.block = fence + 1


def record_resolved_scores(storage: Storage, chain: ReplayChain) -> None:
    windows = compute_fixed_utc_windows(date(2026, 9, 18), offsets=DEFAULT_OFFSETS)
    storage.open_round(
        windows=windows,
        schema_id=RISK_SCHEMA_ID,
        universe=UniverseSnapshot(
            round_id=windows.round_id, tickers=("44",), source_hash="fixture"
        ),
        now_iso=NOW,
    )
    coordinate = risk_coordinate(44, HORIZON_5D_SECONDS, RiskOutput.MAX_DRAWDOWN)
    observations = (
        (chain.hotkeys[1], Decimal("1")),
        (chain.hotkeys[2], Decimal("0.5")),
    )
    values = [
        (hotkey, score, ema_update(None, score, half_life_rounds=5))
        for hotkey, score in observations
    ]
    storage.record_assessment_scoring_pass(
        windows.round_id,
        RISK_SCHEMA_ID,
        horizon_value=HORIZON_5D_SECONDS,
        realized_targets=(),
        output_scores=(),
        ema_updates=[
            AssessmentEmaState(
                miner_hotkey=hotkey, coordinate=coordinate, ema=ema, resolved_rounds=1
            )
            for hotkey, _score, ema in values
        ],
        score_history=[
            AssessmentScoreHistoryRow(
                miner_hotkey=hotkey,
                coordinate=coordinate,
                round_score=score,
                ema_after=ema,
            )
            for hotkey, score, ema in values
        ],
        now_iso=NOW,
    )


def test_bootstrap_hands_off_without_restart_and_never_returns_after_restart(
    storage: Storage, mock_validator_config: bt.Config
) -> None:
    chain = ReplayChain(storage)
    validator = replay_validator(storage, mock_validator_config, chain)
    advance_past_startup_fence(validator, chain)
    validator.set_weights()

    assert chain.submissions == [((176,), (65535,), CURRENT_VERSION_KEY)]
    assert validator.scores == [Decimal("0")] * len(chain.hotkeys)
    assert storage.assessment_ema_states(RISK_SCHEMA_ID) == []
    batch = storage.weight_emission_history(RISK_SCHEMA_ID)[0]
    assert batch["confirmation_state"] == "submitted"
    rows = batch["rows"]
    assert isinstance(rows, list)
    [bootstrap_row] = rows
    assert isinstance(bootstrap_row, dict)
    assert bootstrap_row["blended_score_text"] is None
    assert bootstrap_row["weight_norm_precap_text"] is None

    # Positive resolved state appears in the same running process. An open
    # bootstrap intent still blocks replacement until chain confirmation.
    record_resolved_scores(storage, chain)
    validator._reconstruct_scores()
    validator.set_weights()
    assert len(chain.submissions) == 1
    chain.confirm()
    chain.block += chain.weights_rate_limit
    validator.set_weights()
    assert chain.submissions[-1] == ((1, 2), (65535, 8192), CURRENT_VERSION_KEY)
    assert len(chain.submissions) == 2
    chain.confirm()

    # EMA retirement does not remove score history. Reopening the actual SQLite
    # file reconstructs graduation instead of choosing owner allocation again.
    storage.archive_assessment_ema_horizon(
        RISK_SCHEMA_ID, HORIZON_5D_SECONDS, chain.hotkeys[1:3]
    )
    reopened = Storage.from_url(str(storage._engine.url))
    try:
        chain.storage = reopened
        restarted = replay_validator(reopened, mock_validator_config, chain)
        assert restarted.scores == [Decimal("0")] * len(chain.hotkeys)
        restarted.set_weights()
        assert len(chain.submissions) == 2
        assert restarted._has_positive_score_history is True
    finally:
        reopened.close()


def test_ambiguous_bootstrap_survives_restart_without_duplicate_submission(
    storage: Storage, mock_validator_config: bt.Config
) -> None:
    chain = ReplayChain(storage)
    validator = replay_validator(storage, mock_validator_config, chain)
    advance_past_startup_fence(validator, chain)
    chain.fail_after_submit = True
    validator.set_weights()
    assert (
        storage.weight_emission_history(RISK_SCHEMA_ID)[0]["confirmation_state"]
        == "ambiguous"
    )
    restarted = replay_validator(storage, mock_validator_config, chain)
    restarted.set_weights()
    assert len(chain.submissions) == 1
    chain.confirm()
    chain.fail_after_submit = False
    chain.block += chain.weights_rate_limit
    restarted.set_weights()
    assert chain.submissions == [((176,), (65535,), CURRENT_VERSION_KEY)] * 2


@pytest.mark.parametrize("earned", [False, True])
def test_disable_set_weights_is_an_off_switch_for_both_modes(
    storage: Storage, mock_validator_config: bt.Config, earned: bool
) -> None:
    chain = ReplayChain(storage)
    if earned:
        record_resolved_scores(storage, chain)
    validator = replay_validator(storage, mock_validator_config, chain)
    validator.config.neuron.disable_set_weights = True
    validator.set_weights()
    assert chain.submissions == []
    assert storage.weight_emission_history(RISK_SCHEMA_ID) == []
    assert (
        storage.weight_emission_startup_fence(
            schema_id=RISK_SCHEMA_ID, protocol_version_key=CURRENT_VERSION_KEY
        )
        is None
    )


@pytest.mark.parametrize("changed_identity", ["owner", "recipient", "cached-recipient"])
def test_bootstrap_refuses_recipient_replacement(
    storage: Storage, mock_validator_config: bt.Config, changed_identity: str
) -> None:
    chain = ReplayChain(storage)
    validator = replay_validator(storage, mock_validator_config, chain)
    advance_past_startup_fence(validator, chain)
    if changed_identity == "owner":
        chain.owner_hotkey = "replacement-owner"
    elif changed_identity == "recipient":
        chain.hotkeys[176] = "replacement-miner"
    else:
        validator.metagraph.hotkeys[176] = "stale-recipient"
    with pytest.raises(BootstrapPolicyError):
        validator.set_weights()
    assert chain.submissions == []
    assert not storage.has_open_weight_emission_confirmation(schema_id=RISK_SCHEMA_ID)


@pytest.mark.parametrize("constraint", ["minimum", "maximum"])
def test_bootstrap_obeys_permit_rate_boundary_and_rejects_constraint_changes(
    storage: Storage, mock_validator_config: bt.Config, constraint: str
) -> None:
    chain = ReplayChain(storage)
    validator = replay_validator(storage, mock_validator_config, chain)
    advance_past_startup_fence(validator, chain)
    chain.permits[0] = False
    validator.set_weights()
    assert chain.submissions == []
    chain.permits[0] = True
    chain.last_updates[0] = chain.block - chain.weights_rate_limit + 1
    validator.set_weights()
    assert chain.submissions == []
    chain.block += 1
    if constraint == "minimum":
        chain.minimum = 2
    else:
        chain.maximum = Decimal("0.5")
    with pytest.raises(BootstrapPolicyError):
        validator.set_weights()
    assert chain.submissions == []
    assert not storage.has_open_weight_emission_confirmation(schema_id=RISK_SCHEMA_ID)
    chain.minimum = 1
    chain.maximum = Decimal("1")
    validator.set_weights()
    assert chain.submissions == [((176,), (65535,), CURRENT_VERSION_KEY)]


def test_prepared_before_send_restart_waits_for_expiry_then_recovers(
    storage: Storage, mock_validator_config: bt.Config
) -> None:
    chain = ReplayChain(storage)
    validator = replay_validator(storage, mock_validator_config, chain)
    advance_past_startup_fence(validator, chain)
    chain.crash_before_send = True
    with pytest.raises(SystemExit, match="before transport"):
        validator.set_weights()
    assert chain.submissions == []
    assert storage.weight_emission_history(RISK_SCHEMA_ID)[0]["confirmation_state"] == (
        "prepared"
    )

    restarted = replay_validator(storage, mock_validator_config, chain)
    chain.crash_before_send = False
    restarted.set_weights()
    assert chain.submissions == []
    batch = storage.weight_emission_history(RISK_SCHEMA_ID)[0]
    chain.block = (
        batch["confirmation_deadline_block"]
        + WEIGHT_EMISSION_FINALITY_MARGIN_BLOCKS
        + 1
    )
    result = storage.resolve_weight_emission_confirmations(
        schema_id=RISK_SCHEMA_ID,
        snapshot=WeightEmissionChainSnapshot(
            chain_identity=MAINNET_GENESIS_HASH,
            netuid=30,
            validator_uid=0,
            validator_hotkey=chain.hotkeys[0],
            block=chain.block,
            last_update_block=0,
            weights=(),
            commitments=(),
            reveals=(),
            hotkeys=tuple(enumerate(chain.hotkeys)),
        ),
        finality_margin_blocks=WEIGHT_EMISSION_FINALITY_MARGIN_BLOCKS,
        confirmed_at_iso=NOW,
    )
    assert result.unconfirmed == 1
    restarted.set_weights()
    assert chain.submissions == [((176,), (65535,), CURRENT_VERSION_KEY)]


def test_consistent_post_graduation_backup_preserves_empty_vector_abstention(
    storage: Storage, mock_validator_config: bt.Config, tmp_path: Path
) -> None:
    chain = ReplayChain(storage)
    record_resolved_scores(storage, chain)
    storage.archive_assessment_ema_horizon(
        RISK_SCHEMA_ID, HORIZON_5D_SECONDS, chain.hotkeys[1:3]
    )
    backup_path = tmp_path / "consistent-backup.sqlite"
    database_path = storage._engine.url.database
    assert database_path is not None
    with (
        closing(sqlite3.connect(database_path)) as source,
        closing(sqlite3.connect(backup_path)) as destination,
    ):
        source.backup(destination)

    restored = Storage.from_url(f"sqlite:///{backup_path}")
    try:
        restored_chain = ReplayChain(restored)
        validator = replay_validator(restored, mock_validator_config, restored_chain)
        assert not any(validator.scores)
        validator.set_weights()
        assert restored_chain.submissions == []
        assert validator._has_positive_score_history
        assert restored.weight_emission_history(RISK_SCHEMA_ID) == []
    finally:
        restored.close()
