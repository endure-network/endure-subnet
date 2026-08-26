from __future__ import annotations

from decimal import Decimal

import pytest

from endure.assessment.schemas.forge_lending import (
    COLLATERAL_FACTOR_LIQUIDATION_BUFFER_BPS,
    COLLATERAL_FACTOR_SPEC,
    LIQUIDATION_THRESHOLD_SPEC,
)
from endure.scoring.assessment_orchestrator import score_output
from endure.scoring.lending.observables import collateral_factor_optimal_bps


class TestScoreOutput:
    def test_perfect_collateral_factor_scores_one(self) -> None:
        prices = [Decimal(100), Decimal(40)]
        target = collateral_factor_optimal_bps(
            prices,
            liquidation_buffer_bps=COLLATERAL_FACTOR_LIQUIDATION_BUFFER_BPS,
        )
        assert score_output(target, target, COLLATERAL_FACTOR_SPEC) == Decimal(1)

    def test_dangerously_high_collateral_factor_is_punished(self) -> None:
        prices = [Decimal(100), Decimal(40)]
        target = collateral_factor_optimal_bps(
            prices,
            liquidation_buffer_bps=COLLATERAL_FACTOR_LIQUIDATION_BUFFER_BPS,
        )
        too_high = score_output(target + 1500, target, COLLATERAL_FACTOR_SPEC)
        too_low = score_output(target - 1500, target, COLLATERAL_FACTOR_SPEC)
        assert too_high == Decimal(0)
        assert too_low > Decimal(0)

    def test_non_live_output_is_explicitly_not_implemented(self) -> None:
        with pytest.raises(NotImplementedError):
            score_output(3500, 3500, LIQUIDATION_THRESHOLD_SPEC)
