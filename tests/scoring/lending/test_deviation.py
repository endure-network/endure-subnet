from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from endure.assessment.schemas.forge_lending import (
    COLLATERAL_FACTOR_SPEC,
    SAFE_ASSET_PRICE_SPEC,
    SUPPLY_CAP_SPEC,
    AggressiveDirection,
)
from endure.scoring.assessment_orchestrator import deviation_score


def _score(submitted: int, target: int) -> Decimal:
    return deviation_score(submitted, target, COLLATERAL_FACTOR_SPEC)


class TestDeviationScore:
    def test_inside_grace_band_scores_one(self) -> None:
        assert _score(2600, 2500) == Decimal(1)

    def test_dangerous_side_decays_to_zero_at_cutoff(self) -> None:
        assert _score(2500 + 1500, 2500) == Decimal(0)

    def test_dangerous_side_intermediate_score_is_pinned(self) -> None:
        assert _score(2500 + 850, 2500) == Decimal("0.5")

    def test_dangerous_side_is_penalized_harder_than_lenient(self) -> None:
        over = _score(2500 + 800, 2500)
        under = _score(2500 - 800, 2500)
        assert under > over

    def test_lenient_side_intermediate_score_is_pinned(self) -> None:
        assert deviation_score(2650, 5000, COLLATERAL_FACTOR_SPEC) == Decimal("0.5")

    @pytest.mark.parametrize(
        ("aggressive_direction", "submitted"),
        [
            (AggressiveDirection.HIGHER, 500),
            (AggressiveDirection.LOWER, 9500),
        ],
    )
    def test_lenient_side_reaches_zero_at_expanded_cutoff(
        self, aggressive_direction: AggressiveDirection, submitted: int
    ) -> None:
        spec = replace(
            COLLATERAL_FACTOR_SPEC,
            aggressive_direction=aggressive_direction,
        )
        assert deviation_score(submitted, 5000, spec) == Decimal(0)

    def test_lower_aggressive_direction(self) -> None:
        spec = replace(
            COLLATERAL_FACTOR_SPEC,
            aggressive_direction=AggressiveDirection.LOWER,
        )
        score = deviation_score(1000, 2500, spec)
        assert score == Decimal(0)

    def test_never_negative(self) -> None:
        assert _score(9000, 2500) == Decimal(0)

    def test_relative_mode_is_scale_free(self) -> None:
        small = deviation_score(1_050_000, 1_000_000, SAFE_ASSET_PRICE_SPEC)
        large = deviation_score(
            1_050_000_000_000,
            1_000_000_000_000,
            SAFE_ASSET_PRICE_SPEC,
        )
        assert small == large
        assert Decimal(0) < small < Decimal(1)

    def test_relative_mode_intermediate_score_is_pinned(self) -> None:
        score = deviation_score(1_052_500, 1_000_000, SAFE_ASSET_PRICE_SPEC)
        assert score == Decimal("0.5")

    def test_relative_mode_zero_target_only_rewards_zero(self) -> None:
        def _rel(submitted: int, target: int) -> Decimal:
            return deviation_score(submitted, target, SUPPLY_CAP_SPEC)

        assert _rel(0, 0) == Decimal(1)
        assert _rel(5, 0) == Decimal(0)
