"""Alpha Risk R5 mock cycle: real commit/reveal handlers, compressed clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from endure.assessment.schemas.subnet_alpha_risk import (
    RISK_HORIZONS,
    RISK_SCHEMA_ID,
    RiskOutput,
    RiskSubmissionBundle,
)
from endure.assessment.subnet_alpha_universe import (
    ALPHA_RISK_WHITELISTED_NETUIDS,
    StaticAlphaRiskUniverseProvider,
)
from endure.protocol.handlers import SubmissionHandlers
from endure.protocol.miner_service import MinerRoundService
from endure.protocol.risk_miner import RiskBaselineAssembler
from endure.protocol.risk_runtime import RECORDED_FIXTURE_WINDOW_START_BLOCK
from endure.protocol.schedulers import SyntheticScheduler
from endure.protocol.synapses import SubmitCommit, SubmitReveal
from endure.protocol.validator_service import ValidatorRoundService
from endure.protocol.vertical import AssessmentRoundProgram
from endure.scoring.assessment_orchestrator import (
    REALIZED_TARGET_RESOLVED,
    REALIZED_TARGET_VOIDED,
)
from endure.scoring.market_data import recorded_mainnet_fixture_provider
from endure.scoring.policy import DEFAULT_PAYOUT_HALF_LIFE_ROUNDS
from endure.scoring.risk.orchestrator import RiskScoringOrchestrator
from endure.storage.repository import Storage

EPOCH = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)
ROUND_ID = EPOCH.date().isoformat()
MINER_HOTKEY = "hk-risk-miner"


async def test_alpha_risk_mock_cycle_runs_through_real_handlers(
    storage: Storage,
) -> None:
    """Compressed time controls WHEN; fixture block windows control WHAT resolves."""
    now_holder = {"now": EPOCH + timedelta(seconds=1)}
    provider = recorded_mainnet_fixture_provider()
    scheduler = SyntheticScheduler(
        sessions=(EPOCH.date(),), epoch=EPOCH, period_seconds=20
    )
    handlers = SubmissionHandlers(
        storage=storage,
        schema_id=RISK_SCHEMA_ID,
        now_fn=lambda: now_holder["now"],
    )

    async def send(synapse: SubmitCommit | SubmitReveal) -> int:
        if isinstance(synapse, SubmitCommit):
            response = await handlers.handle_commit(synapse, miner_hotkey=MINER_HOTKEY)
        else:
            response = await handlers.handle_reveal(synapse, miner_hotkey=MINER_HOTKEY)
        return 1 if response.accepted else 0

    miner = MinerRoundService(
        scheduler=scheduler,
        assemble=RiskBaselineAssembler(
            netuids=ALPHA_RISK_WHITELISTED_NETUIDS,
            miner_hotkey=MINER_HOTKEY,
            latest_observation=provider.latest_pool_observation,
        ),
        send=send,
        now_fn=lambda: now_holder["now"],
    )
    risk = RiskScoringOrchestrator(
        storage=storage,
        price_provider=provider,
        half_life_rounds=DEFAULT_PAYOUT_HALF_LIFE_ROUNDS,
        reveal_close_block=lambda reveal_close: RECORDED_FIXTURE_WINDOW_START_BLOCK,
    )
    validator = ValidatorRoundService(
        storage=storage,
        scheduler=scheduler,
        universe_provider=StaticAlphaRiskUniverseProvider(),
        schema_id=RISK_SCHEMA_ID,
        horizons=RISK_HORIZONS,
        now_fn=lambda: now_holder["now"],
        round_program=AssessmentRoundProgram(
            storage=storage,
            schema_id=RISK_SCHEMA_ID,
            bundle_model=RiskSubmissionBundle,
            orchestrator=risk,
            horizons=RISK_HORIZONS,
            due_seconds_by_horizon={RISK_HORIZONS[0]: 1, RISK_HORIZONS[1]: 2},
        ),
    )

    validator.tick(expected_miners=(MINER_HOTKEY,))
    assert storage.round_state(ROUND_ID, RISK_SCHEMA_ID) == "open"

    await miner.tick()
    assert storage.committed_hash(ROUND_ID, RISK_SCHEMA_ID, MINER_HOTKEY) is not None

    now_holder["now"] = EPOCH + timedelta(seconds=12)
    await miner.tick()
    accepted = storage.accepted_assessment_bundles(ROUND_ID, RISK_SCHEMA_ID)
    assert [bundle.miner_hotkey for bundle in accepted] == [MINER_HOTKEY]

    now_holder["now"] = EPOCH + timedelta(seconds=16)
    validator.tick(expected_miners=(MINER_HOTKEY,))
    assert storage.round_state(ROUND_ID, RISK_SCHEMA_ID) == "partially_scored"
    assert storage.has_assessment_resolution_marker(
        ROUND_ID, RISK_SCHEMA_ID, RISK_HORIZONS[0]
    )

    now_holder["now"] = EPOCH + timedelta(seconds=17)
    weights = validator.tick(expected_miners=(MINER_HOTKEY,))
    assert storage.round_state(ROUND_ID, RISK_SCHEMA_ID) == "closed"
    assert weights is not None and weights[MINER_HOTKEY] > Decimal(0)

    consensus = storage.assessment_consensus_for(ROUND_ID, RISK_SCHEMA_ID)
    assert len(consensus) == 2 * len(tuple(RiskOutput)) * len(RISK_HORIZONS)
    targets = storage.assessment_realized_targets_for(ROUND_ID, RISK_SCHEMA_ID)
    resolved_netuids = {
        int(target.coordinate.target_id)
        for target in targets
        if target.status == REALIZED_TARGET_RESOLVED
    }
    voided_netuids = {
        int(target.coordinate.target_id)
        for target in targets
        if target.status == REALIZED_TARGET_VOIDED
    }
    assert resolved_netuids == {8, 44}
    assert voided_netuids == set(ALPHA_RISK_WHITELISTED_NETUIDS) - {8, 44}
    assert len(targets) == len(ALPHA_RISK_WHITELISTED_NETUIDS) * len(
        tuple(RiskOutput)
    ) * len(RISK_HORIZONS)
    assert all(
        state.ema > Decimal(0)
        for state in storage.assessment_ema_states(RISK_SCHEMA_ID)
    )
