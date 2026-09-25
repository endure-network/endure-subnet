"""`make dev` / --mock with weight setting on reaches the mock chain's set_weights."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import bittensor as bt
from bittensor.core.types import ExtrinsicResponse

from endure.assessment.coordinates import AssessmentEmaState, AssessmentScoreHistoryRow
from endure.assessment.registry import UniverseSnapshot
from endure.assessment.schemas.subnet_alpha_risk import (
    HORIZON_5D_SECONDS,
    RISK_SCHEMA_ID,
    RiskOutput,
)
from endure.protocol.round_engine import DEFAULT_OFFSETS, compute_fixed_utc_windows
from endure.runtime.mock import MockSubtensor
from endure.scoring.risk.orchestrator import risk_coordinate
from endure.scoring.weights import ema_update
from endure.storage.repository import Storage
from neurons.validator import Validator

NOW = "2026-09-24T20:00:00+00:00"


class MockClockValidator(Validator):
    @property
    def block(self) -> int:
        # Read the mock chain clock directly, not through the 12 s TTL cache.
        return self.subtensor.get_current_block()


def _record_positive_emas(storage: Storage, hotkeys: Sequence[str]) -> None:
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
    values = [
        (hotkey, score, ema_update(None, score, half_life_rounds=5))
        for hotkey, score in zip(hotkeys, (Decimal("1"), Decimal("0.5")), strict=True)
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


def test_mock_validator_sets_earned_weights_on_the_mock_chain(
    mock_validator_config: bt.Config,
) -> None:
    config = mock_validator_config
    config.neuron.disable_set_weights = False
    config.neuron.axon_off = True
    config.neuron.epoch_length = 2
    submitted: list[tuple[list[int], list[int]]] = []
    include = MockSubtensor.set_weights

    def spy(
        self: MockSubtensor,
        wallet: bt.Wallet,
        netuid: int,
        uids: list[int],
        weights: list[int],
        version_key: int = 0,
        **kwargs: object,
    ) -> ExtrinsicResponse:
        submitted.append((list(uids), list(weights)))
        return include(self, wallet, netuid, uids, weights, version_key, **kwargs)

    validator = MockClockValidator(config=config)
    try:
        chain = validator.metagraph.subtensor
        assert isinstance(chain, MockSubtensor)
        _record_positive_emas(validator._storage, validator.metagraph.hotkeys[1:3])
        validator.step = 1
        with patch.object(MockSubtensor, "set_weights", spy):
            assert validator.should_set_weights() is False  # seeds epoch pacing
            for _ in range(3):
                chain.do_block_step()
            assert validator.should_set_weights() is True
            validator.set_weights()

        # Earned weight reaches exactly the two scored miners.
        assert [uids for uids, _weights in submitted] == [[1, 2]]
        assert all(weight > 0 for _uids, weights in submitted for weight in weights)
        assert validator._emission_reason == "confirmation_pending"
        [batch] = validator._storage.weight_emission_history(RISK_SCHEMA_ID)
        assert batch["status"] == "submitted"
    finally:
        validator.close_transport_resources()
