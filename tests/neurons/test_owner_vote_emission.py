"""One durable emitter alternates between the owner vote and earned weights."""

from __future__ import annotations

import copy
import re
import sqlite3
from contextlib import closing
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import bittensor as bt
import numpy as np
import pytest
from bittensor.core.chain_data.metagraph_info import SelectiveMetagraphIndex
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
from endure.base.validator import (
    WEIGHT_EMISSION_FINALITY_MARGIN_BLOCKS,
    BaseValidatorNeuron,
)
from endure.protocol.consensus_policy import (
    MAINNET_GENESIS_HASH,
    SN30_NETUID,
    SN30_OWNER_HOTKEY,
)
from endure.protocol.round_engine import DEFAULT_OFFSETS, compute_fixed_utc_windows
from endure.protocol.schedulers import FixedUtcScheduler
from endure.protocol.version_contract import CURRENT_VERSION_KEY
from endure.protocol.vertical import AssessmentRoundProgram, VerticalRuntime
from endure.scoring.market_data import recorded_mainnet_fixture_provider
from endure.scoring.risk.orchestrator import RiskScoringOrchestrator, risk_coordinate
from endure.scoring.weights import ema_update
from endure.storage.repository import Storage, WeightEmissionChainSnapshot
from neurons.validator import Validator
from tests.neurons.test_emission_health import _check_storage_calls

NOW = "2026-09-24T20:00:00+00:00"
TESTNET_GENESIS = "0xtestnet-genesis"
OWNER_VOTE_176 = ((176,), (65535,), CURRENT_VERSION_KEY)
EARNED = ((1, 2), (65535, 8192), CURRENT_VERSION_KEY)


def _snake_case(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


class ReplayChain:
    """Stateful SDK boundary; no network, wallet signing, or chain mutation."""

    def __init__(
        self,
        storage: Storage,
        *,
        owner_uid: int = 176,
        owner_hotkey: str = SN30_OWNER_HOTKEY,
        genesis: str = MAINNET_GENESIS_HASH,
        netuid: int = SN30_NETUID,
    ) -> None:
        self.storage = storage
        self.genesis = genesis
        self.netuid = netuid
        self.block = 1_000
        self.hotkeys = [f"hotkey-{uid}" for uid in range(max(owner_uid, 176) + 1)]
        self.hotkeys[owner_uid] = owner_hotkey
        self.owner_hotkey = owner_hotkey
        self.permits = [True] + [False] * (len(self.hotkeys) - 1)
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
        return self.genesis

    def commit_reveal_enabled(self, *, netuid: int) -> bool:
        assert netuid == self.netuid
        return False

    def min_allowed_weights(self, *, netuid: int) -> int:
        return self.minimum

    def max_weight_limit(self, *, netuid: int) -> Decimal:
        return self.maximum

    def get_metagraph_info(
        self, netuid: int, *, selected_indices: list[int], block: int
    ) -> SimpleNamespace:
        # Like the SDK's selective runtime call, unrequested fields stay None.
        assert netuid == self.netuid and block == self.block
        fields = {
            SelectiveMetagraphIndex.Block: block,
            SelectiveMetagraphIndex.Hotkeys: list(self.hotkeys),
            SelectiveMetagraphIndex.OwnerHotkey: self.owner_hotkey,
            SelectiveMetagraphIndex.ValidatorPermit: list(self.permits),
            SelectiveMetagraphIndex.LastUpdate: list(self.last_updates),
            SelectiveMetagraphIndex.WeightsRateLimit: self.weights_rate_limit,
        }
        return SimpleNamespace(
            **{
                _snake_case(index.name): value
                if index.value in selected_indices
                else None
                for index, value in fields.items()
            }
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

    def resolve(self, *, confirmed: bool) -> None:
        self.block = self.last_updates[0] + WEIGHT_EMISSION_FINALITY_MARGIN_BLOCKS + 1
        result = self.storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=WeightEmissionChainSnapshot(
                chain_identity=self.genesis,
                netuid=self.netuid,
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
        assert (result.confirmed if confirmed else result.unconfirmed) == 1

    def confirm_and_pace(self) -> None:
        self.resolve(confirmed=True)
        self.block += self.weights_rate_limit


class ReplayValidator(Validator):
    @property
    def block(self) -> int:
        # A virtual chain clock must not wait for the live adapter's TTL cache.
        return self.subtensor.get_current_block()


def replay_validator(
    storage: Storage,
    config: bt.Config,
    chain: ReplayChain,
    *,
    network: str = "finney",
) -> ReplayValidator:
    validator = ReplayValidator.__new__(ReplayValidator)
    validator.config = copy.deepcopy(config)
    validator.config.runtime.mode = "live"
    validator.config.subtensor.network = network
    validator.config.netuid = chain.netuid
    validator.config.endure.active_schema = RISK_SCHEMA_ID
    validator.config.endure.serving_stage = "mainnet"
    validator.config.neuron.disable_set_weights = False
    validator.config.neuron.axon_off = False
    validator._storage = storage
    validator._schema_id = RISK_SCHEMA_ID
    validator._owner_vote_recipient = None
    validator._emission_block = None
    validator._emission_block_since_block = None
    validator._emission_block_seen_block = None
    validator._durable_scores_loaded = True
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
        scheduler=FixedUtcScheduler(),
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


def archive_scored_miners(storage: Storage, chain: ReplayChain) -> None:
    storage.archive_assessment_ema_horizon(
        RISK_SCHEMA_ID, HORIZON_5D_SECONDS, chain.hotkeys[1:3]
    )


def test_owner_vote_hands_off_to_scores_and_returns_when_all_miners_archive(
    storage: Storage, mock_validator_config: bt.Config
) -> None:
    chain = ReplayChain(storage)
    validator = replay_validator(storage, mock_validator_config, chain)
    advance_past_startup_fence(validator, chain)
    validator.set_weights()

    assert chain.submissions == [OWNER_VOTE_176]
    assert validator.scores == [Decimal("0")] * len(chain.hotkeys)
    assert storage.assessment_ema_states(RISK_SCHEMA_ID) == []
    [owner_row] = storage.weight_emission_history(RISK_SCHEMA_ID)[0]["rows"]
    assert owner_row["blended_score_text"] is None
    assert owner_row["weight_norm_precap_text"] is None

    # The first positive score in the same process switches to earned weights
    # once the open owner-vote intent is confirmed.
    record_resolved_scores(storage, chain)
    validator._reconstruct_scores()
    validator.set_weights()
    assert chain.submissions == [OWNER_VOTE_176]
    chain.confirm_and_pace()
    validator.set_weights()
    assert chain.submissions == [OWNER_VOTE_176, EARNED]
    assert validator._observed_emission_mode() == "scored"

    # Every scored miner archived: the owner vote is a standing fallback.
    archive_scored_miners(storage, chain)
    validator._reconstruct_scores()
    assert validator._observed_emission_mode() == "owner_vote"
    chain.confirm_and_pace()
    validator.set_weights()
    assert chain.submissions == [OWNER_VOTE_176, EARNED, OWNER_VOTE_176]


def test_confirmed_deregistration_archival_returns_to_owner_vote(
    storage: Storage, mock_validator_config: bt.Config
) -> None:
    chain = ReplayChain(storage)
    record_resolved_scores(storage, chain)
    validator = replay_validator(storage, mock_validator_config, chain)
    validator._seed_deregistration_tracker()
    tracker = validator._deregistration_tracker()
    advance_past_startup_fence(validator, chain)
    validator.set_weights()
    assert chain.submissions == [EARNED]

    remaining = [hotkey for hotkey in chain.hotkeys if hotkey not in chain.hotkeys[1:3]]
    tracker.advance(remaining)
    assert tracker.confirmed() == []
    tracker.advance(remaining)
    storage.archive_assessment_ema_horizon(
        RISK_SCHEMA_ID, HORIZON_5D_SECONDS, tracker.confirmed()
    )
    validator._reconstruct_scores()
    chain.confirm_and_pace()
    validator.set_weights()

    assert chain.submissions == [EARNED, OWNER_VOTE_176]


def test_restart_while_scored_resumes_earned_weights_without_owner_flicker(
    storage: Storage, mock_validator_config: bt.Config
) -> None:
    chain = ReplayChain(storage)
    record_resolved_scores(storage, chain)
    validator = replay_validator(storage, mock_validator_config, chain)
    advance_past_startup_fence(validator, chain)
    validator.set_weights()
    chain.confirm_and_pace()

    reopened = Storage.from_url(str(storage._engine.url))
    try:
        chain.storage = reopened
        restarted = replay_validator(reopened, mock_validator_config, chain)
        assert restarted._observed_emission_mode() == "scored"
        restarted.set_weights()
        assert restarted._observed_emission_mode() == "scored"
        assert chain.submissions == [EARNED, EARNED]
    finally:
        reopened.close()


@pytest.mark.parametrize(
    ("network", "genesis", "netuid", "owner_hotkey"),
    [
        ("finney", MAINNET_GENESIS_HASH, SN30_NETUID, SN30_OWNER_HOTKEY),
        ("test", TESTNET_GENESIS, 417, "testnet-owner"),
    ],
)
def test_owner_vote_follows_the_on_chain_owner_to_any_uid(
    storage: Storage,
    mock_validator_config: bt.Config,
    network: str,
    genesis: str,
    netuid: int,
    owner_hotkey: str,
) -> None:
    chain = ReplayChain(
        storage, owner_uid=5, owner_hotkey=owner_hotkey, genesis=genesis, netuid=netuid
    )
    validator = replay_validator(storage, mock_validator_config, chain, network=network)
    advance_past_startup_fence(validator, chain)
    validator.set_weights()

    assert chain.submissions == [((5,), (65535,), CURRENT_VERSION_KEY)]


def test_testnet_owner_change_moves_the_vote_to_the_new_owner(
    storage: Storage, mock_validator_config: bt.Config
) -> None:
    chain = ReplayChain(
        storage,
        owner_uid=5,
        owner_hotkey="testnet-owner",
        genesis=TESTNET_GENESIS,
        netuid=417,
    )
    validator = replay_validator(storage, mock_validator_config, chain, network="test")
    advance_past_startup_fence(validator, chain)
    chain.owner_hotkey = chain.hotkeys[9] = "new-testnet-owner"
    validator.metagraph.hotkeys[9] = "new-testnet-owner"
    validator.set_weights()

    assert chain.submissions == [((9,), (65535,), CURRENT_VERSION_KEY)]


def test_mainnet_owner_mismatch_abstains_until_the_pinned_owner_returns(
    storage: Storage, mock_validator_config: bt.Config
) -> None:
    mainnet = ReplayChain(storage)
    validator = replay_validator(storage, mock_validator_config, mainnet)
    advance_past_startup_fence(validator, mainnet)
    mainnet.owner_hotkey = mainnet.hotkeys[9] = "replacement-owner"
    validator.metagraph.hotkeys[9] = "replacement-owner"
    validator.set_weights()

    assert mainnet.submissions == []
    assert validator._observed_emission_mode() == "abstain"
    assert validator._emission_reason == "owner_hotkey_mismatch"
    assert validator._consecutive_set_weights_failures == 0
    assert storage.weight_emission_history(RISK_SCHEMA_ID) == []

    # Restoring the pinned owner clears the block on the next attempt.
    mainnet.owner_hotkey = SN30_OWNER_HOTKEY
    validator.set_weights()
    assert mainnet.submissions == [OWNER_VOTE_176]
    assert validator._observed_emission_mode() == "owner_vote"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("unregistered", "owner_unregistered"),
        ("stale-local-metagraph", "owner_snapshot_inconsistent"),
        ("stale-snapshot-block", "chain_snapshot_inconsistent"),
        ("wrong-chain", "owner_vote_chain_mismatch"),
    ],
)
def test_unsafe_owner_state_abstains_with_its_reason(
    storage: Storage, mock_validator_config: bt.Config, change: str, reason: str
) -> None:
    chain = ReplayChain(storage)
    validator = replay_validator(storage, mock_validator_config, chain)
    advance_past_startup_fence(validator, chain)
    if change == "unregistered":
        chain.hotkeys[176] = "replacement-miner"
    elif change == "stale-local-metagraph":
        validator.metagraph.hotkeys[176] = "stale-recipient"
    elif change == "stale-snapshot-block":
        original = chain.get_metagraph_info

        def stale(
            netuid: int, *, selected_indices: list[int], block: int
        ) -> SimpleNamespace:
            snapshot = original(netuid, selected_indices=selected_indices, block=block)
            snapshot.block = block - 1
            return snapshot

        chain.get_metagraph_info = stale
    else:
        chain.genesis = TESTNET_GENESIS
    validator.set_weights()

    assert chain.submissions == []
    assert validator._observed_emission_mode() == "abstain"
    assert validator._emission_reason == reason
    assert not storage.has_open_weight_emission_confirmation(schema_id=RISK_SCHEMA_ID)


@pytest.mark.parametrize("network", ["local", "mock"])
def test_local_and_mock_networks_keep_abstaining(
    storage: Storage, mock_validator_config: bt.Config, network: str
) -> None:
    chain = ReplayChain(storage)
    validator = replay_validator(storage, mock_validator_config, chain, network=network)
    if network == "mock":
        validator.config.runtime.mode = "mock"
    validator.set_weights()
    validator.set_weights()

    assert chain.submissions == []
    assert validator._observed_emission_mode() == "abstain"
    assert validator._emission_reason == "no_positive_scores"


def test_local_network_still_emits_earned_weights_when_scored(
    storage: Storage, mock_validator_config: bt.Config
) -> None:
    chain = ReplayChain(storage)
    record_resolved_scores(storage, chain)
    validator = replay_validator(storage, mock_validator_config, chain, network="local")
    advance_past_startup_fence(validator, chain)
    validator.set_weights()

    assert chain.submissions == [EARNED]


def test_ambiguous_owner_vote_survives_restart_without_duplicate_submission(
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
    chain.fail_after_submit = False
    chain.confirm_and_pace()
    restarted.set_weights()
    assert chain.submissions == [OWNER_VOTE_176] * 2


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


@pytest.mark.parametrize("constraint", ["minimum", "maximum"])
def test_owner_vote_obeys_permit_strict_rate_limit_and_rejects_constraint_changes(
    storage: Storage, mock_validator_config: bt.Config, constraint: str
) -> None:
    chain = ReplayChain(storage)
    validator = replay_validator(storage, mock_validator_config, chain)
    advance_past_startup_fence(validator, chain)
    chain.permits[0] = False
    validator.set_weights()
    assert chain.submissions == []
    assert validator._emission_reason == "no_validator_permit"
    chain.permits[0] = True
    # Subtensor's limit is strict: at exactly last_update + limit the SDK
    # refuses, so the attempt must defer instead of recording a failure.
    chain.last_updates[0] = chain.block - chain.weights_rate_limit
    validator.set_weights()
    assert chain.submissions == []
    assert validator._emission_reason == "chain_rate_limit"
    assert validator._consecutive_set_weights_failures == 0
    chain.block += 1
    if constraint == "minimum":
        chain.minimum = 2
    else:
        chain.maximum = Decimal("0.5")
    # A refused recheck counts one failed attempt and returns to sync(), which
    # advances the attempt block: no hot retry loop.
    validator.set_weights()
    assert chain.submissions == []
    assert not storage.has_open_weight_emission_confirmation(schema_id=RISK_SCHEMA_ID)
    assert validator._emission_reason == "owner_vote_vector_invalid"
    assert validator._consecutive_set_weights_failures == 1
    chain.minimum = 1
    chain.maximum = Decimal("1")
    validator.set_weights()
    assert chain.submissions == [OWNER_VOTE_176]


def test_refused_recheck_is_durable_across_a_restart(
    storage: Storage, mock_validator_config: bt.Config
) -> None:
    chain = ReplayChain(storage)
    validator = replay_validator(storage, mock_validator_config, chain)
    advance_past_startup_fence(validator, chain)
    chain.minimum = 2

    validator.set_weights()

    assert chain.submissions == []
    assert validator._emission_reason == "owner_vote_vector_invalid"
    restarted = Storage.from_url(str(storage._engine.url))
    try:
        [batch] = restarted.weight_emission_history(RISK_SCHEMA_ID)
        assert (batch["status"], batch["confirmation_state"]) == ("failed", "failed")
        assert batch["confirmation_deadline_block"] is None
        # The refused vector is recorded as prepared, and none of it was sent.
        assert max(row["weight_u16"] for row in batch["rows"]) == 65535
        assert not any(row["emitted"] for row in batch["rows"])
        health = restarted.weight_emission_confirmation_health(
            schema_id=RISK_SCHEMA_ID, current_block=chain.block
        )
        assert health.failed_submissions_total == 1
        assert health.open_submissions == 0
        # The refusal holds no confirmation slot: the next attempt sends.
        chain.minimum = 1
        rerun = replay_validator(restarted, mock_validator_config, chain)
        rerun.set_weights()
        assert chain.submissions == [OWNER_VOTE_176]
    finally:
        restarted.close()


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
            netuid=SN30_NETUID,
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
    assert chain.submissions == [OWNER_VOTE_176]


@pytest.mark.parametrize(
    ("archived", "expected"),
    [(False, EARNED), (True, OWNER_VOTE_176)],
)
def test_restored_backup_reproduces_its_own_scoring_state(
    storage: Storage,
    mock_validator_config: bt.Config,
    tmp_path: Path,
    archived: bool,
    expected: tuple[tuple[int, ...], tuple[int, ...], int],
) -> None:
    chain = ReplayChain(storage)
    record_resolved_scores(storage, chain)
    if archived:
        archive_scored_miners(storage, chain)
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
        advance_past_startup_fence(validator, restored_chain)
        validator.set_weights()
        assert restored_chain.submissions == [expected]
    finally:
        restored.close()


def test_scored_mode_defers_at_the_strict_chain_rate_limit(
    storage: Storage, mock_validator_config: bt.Config
) -> None:
    chain = ReplayChain(storage)
    record_resolved_scores(storage, chain)
    validator = replay_validator(storage, mock_validator_config, chain)
    advance_past_startup_fence(validator, chain)
    chain.last_updates[0] = chain.block - chain.weights_rate_limit

    validator.set_weights()

    assert chain.submissions == []
    assert validator._emission_reason == "chain_rate_limit"
    assert validator._consecutive_set_weights_failures == 0
    assert storage.weight_emission_history(RISK_SCHEMA_ID) == []
    chain.block += 1
    validator.set_weights()
    assert chain.submissions == [EARNED]


def _reregister_scored_miners(validator: ReplayValidator, chain: ReplayChain) -> None:
    """Miners 1 and 2 re-register at UIDs 3 and 4, as a resync would align it."""
    moved = {3: chain.hotkeys[1], 4: chain.hotkeys[2]}
    chain.hotkeys[1], chain.hotkeys[2] = "newcomer-1", "newcomer-2"
    for uid, hotkey in moved.items():
        chain.hotkeys[uid] = hotkey
    validator.metagraph.hotkeys = list(chain.hotkeys)
    validator.scores = [Decimal(0)] * len(chain.hotkeys)


def _base_resync(validator: ReplayValidator, chain: ReplayChain) -> None:
    """Base resync order: align (zeroing moved UIDs), then the post-sync hook."""
    _reregister_scored_miners(validator, chain)
    validator._on_metagraph_synced()


def test_resync_after_reregistration_keeps_earned_weights(
    storage: Storage,
    mock_validator_config: bt.Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain = ReplayChain(storage)
    record_resolved_scores(storage, chain)
    validator = replay_validator(storage, mock_validator_config, chain)
    advance_past_startup_fence(validator, chain)
    monkeypatch.setattr(
        BaseValidatorNeuron,
        "resync_metagraph",
        lambda self: _base_resync(self, chain),
    )
    # The confirmation RPCs are where /health can poll mid-resync.
    modes_during_confirmation: list[str] = []
    monkeypatch.setattr(
        validator,
        "_resolve_weight_confirmations",
        lambda: modes_during_confirmation.append(validator._observed_emission_mode()),
    )

    validator.resync_metagraph()

    assert modes_during_confirmation == ["scored"]
    assert validator._observed_emission_mode() == "scored"
    validator.set_weights()
    assert chain.submissions == [((3, 4), (65535, 8192), CURRENT_VERSION_KEY)]


def test_set_weights_rebuilds_a_zeroed_vector_from_durable_state(
    storage: Storage, mock_validator_config: bt.Config
) -> None:
    chain = ReplayChain(storage)
    record_resolved_scores(storage, chain)
    validator = replay_validator(storage, mock_validator_config, chain)
    advance_past_startup_fence(validator, chain)
    _reregister_scored_miners(validator, chain)

    validator.set_weights()

    assert chain.submissions == [((3, 4), (65535, 8192), CURRENT_VERSION_KEY)]


def test_unreadable_durable_scores_defer_instead_of_emitting(
    storage: Storage,
    mock_validator_config: bt.Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain = ReplayChain(storage)
    record_resolved_scores(storage, chain)
    validator = replay_validator(storage, mock_validator_config, chain)
    advance_past_startup_fence(validator, chain)

    def unreadable() -> dict[str, Decimal]:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(
        validator,
        "_vertical_runtime",
        SimpleNamespace(
            round_program=SimpleNamespace(weights=unreadable, blended_scores=dict)
        ),
    )
    validator.set_weights()

    assert chain.submissions == []
    assert validator._emission_reason == "score_state_unavailable"
    assert validator._observed_emission_mode() == "abstain"
    # Persisting across two epochs of attempts escalates like any other block.
    assert not validator._emission_block_degraded()
    chain.block += 2 * int(validator.config.neuron.epoch_length)
    validator.set_weights()
    assert validator._emission_block_degraded()


def test_failed_resync_rebuild_never_reports_owner_vote(
    storage: Storage,
    mock_validator_config: bt.Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain = ReplayChain(storage)
    record_resolved_scores(storage, chain)
    validator = replay_validator(storage, mock_validator_config, chain)
    monkeypatch.setattr(
        BaseValidatorNeuron,
        "resync_metagraph",
        lambda self: _base_resync(self, chain),
    )

    def unreadable() -> dict[str, Decimal]:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(
        validator,
        "_vertical_runtime",
        SimpleNamespace(
            round_program=SimpleNamespace(weights=unreadable, blended_scores=dict)
        ),
    )
    monkeypatch.setattr(validator, "_resolve_weight_confirmations", lambda: None)
    validator.resync_metagraph()

    # The aligned vector is all zero, but durable positive EMAs exist.
    assert not any(validator.scores)
    assert validator._observed_emission_mode() == "abstain"
    assert validator._emission_reason == "score_state_unavailable"


def test_scored_weight_never_follows_a_uid_reregistered_on_chain(
    storage: Storage, mock_validator_config: bt.Config
) -> None:
    chain = ReplayChain(storage)
    record_resolved_scores(storage, chain)
    validator = replay_validator(storage, mock_validator_config, chain)
    advance_past_startup_fence(validator, chain)
    # UID 1 changed hands on chain; the local metagraph has not resynced yet.
    chain.hotkeys[1] = "new-registrant"

    validator.set_weights()

    assert chain.submissions == []
    assert validator._emission_reason == "chain_snapshot_inconsistent"


def test_emission_cycle_never_touches_storage_under_the_emission_lock(
    storage: Storage, mock_validator_config: bt.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _check_storage_calls(storage, monkeypatch)
    chain = ReplayChain(storage)
    record_resolved_scores(storage, chain)
    validator = replay_validator(storage, mock_validator_config, chain)
    advance_past_startup_fence(validator, chain)

    validator.set_weights()  # prepare, submit
    chain.confirm_and_pace()
    validator.set_weights()  # the next attempt
    chain.confirm_and_pace()

    assert chain.submissions == [EARNED, EARNED]
    assert {"record_weight_emission", "transition_weight_emission_attempt"} <= set(
        calls
    )
