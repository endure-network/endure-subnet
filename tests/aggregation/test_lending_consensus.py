"""Generic lending consensus over the assessment-coordinate spine (Batch 7)."""

from __future__ import annotations

from decimal import Decimal

from endure.aggregation.assessment_consensus import compute_assessment_consensus
from endure.aggregation.consensus import MIN_CONSENSUS_WEIGHT, weighted_median
from endure.assessment.coordinates import AssessmentCoordinate
from endure.assessment.schemas.forge_lending import (
    LENDING_HORIZON_SECONDS,
    LendingAssetSubmission,
    LendingOutput,
    LendingOutputValue,
    LendingSubmissionBundle,
)

ROUND = "2026-07-06"
_UNITS = {
    LendingOutput.SAFE_ASSET_PRICE: "price_1e18",
    LendingOutput.COLLATERAL_FACTOR: "ltv_bps",
    LendingOutput.LIQUIDATION_THRESHOLD: "ltv_bps",
    LendingOutput.LIQUIDATION_INCENTIVE: "mantissa_1e18",
    LendingOutput.SUPPLY_CAP: "underlying_units",
    LendingOutput.BORROW_CAP: "tao_units",
    LendingOutput.RISK_TIER: "ordinal",
}


def _bundle(*, netuid: int, collateral_factor: int) -> LendingSubmissionBundle:
    values = {
        LendingOutput.SAFE_ASSET_PRICE: 10**16,
        LendingOutput.COLLATERAL_FACTOR: collateral_factor,
        LendingOutput.LIQUIDATION_THRESHOLD: collateral_factor + 1000,
        LendingOutput.LIQUIDATION_INCENTIVE: 10**18 + 10**17,
        LendingOutput.SUPPLY_CAP: 0,
        LendingOutput.BORROW_CAP: 0,
        LendingOutput.RISK_TIER: 6,
    }
    outputs = tuple(
        LendingOutputValue(
            output=output,
            value=value,
            confidence_bps=8000,
            reason_codes=("thin_liquidity",),
            horizon_seconds=LENDING_HORIZON_SECONDS,
            unit=_UNITS[output],
        )
        for output, value in values.items()
    )
    return LendingSubmissionBundle(
        round_id=ROUND,
        schema_id="lending.v1.subnet_asset",
        assets=(LendingAssetSubmission(netuid=netuid, outputs=outputs),),
    )


class TestComputeAssessmentConsensus:
    def test_produces_one_row_per_netuid_output_on_coordinate_spine(self) -> None:
        bundles = {"hk-a": _bundle(netuid=44, collateral_factor=5000)}

        rows = compute_assessment_consensus(bundles, blends={})

        assert len(rows) == 7
        cf_row = next(
            row
            for row in rows
            if row.coordinate.output == LendingOutput.COLLATERAL_FACTOR.value
        )
        assert cf_row.coordinate == AssessmentCoordinate.subnet_asset(
            netuid=44,
            horizon_seconds=LENDING_HORIZON_SECONDS,
            output=LendingOutput.COLLATERAL_FACTOR.value,
        )
        assert cf_row.value == Decimal(5000)
        assert cf_row.n_submitters == 1

    def test_weighted_median_of_collateral_factor(self) -> None:
        bundles = {
            "hk-a": _bundle(netuid=44, collateral_factor=5000),
            "hk-b": _bundle(netuid=44, collateral_factor=5200),
            "hk-c": _bundle(netuid=44, collateral_factor=6000),
        }

        rows = compute_assessment_consensus(bundles, blends={})

        cf_row = next(
            row
            for row in rows
            if row.coordinate.output == LendingOutput.COLLATERAL_FACTOR.value
        )
        # Equal (floored) weights → the middle of three sorted values.
        assert cf_row.value == Decimal(5200)
        assert cf_row.n_submitters == 3
        assert cf_row.dispersion >= 0

    def test_is_order_independent(self) -> None:
        a = _bundle(netuid=44, collateral_factor=5000)
        b = _bundle(netuid=8, collateral_factor=5500)

        first = compute_assessment_consensus({"hk-a": a, "hk-b": b}, blends={})
        second = compute_assessment_consensus({"hk-b": b, "hk-a": a}, blends={})

        assert first == second

    def test_blend_weights_shift_the_median(self) -> None:
        bundles = {
            "hk-low": _bundle(netuid=44, collateral_factor=5000),
            "hk-high": _bundle(netuid=44, collateral_factor=6000),
        }
        # Heavily weight the high submitter past the median tipping point.
        rows = compute_assessment_consensus(
            bundles, blends={"hk-high": Decimal("10"), "hk-low": MIN_CONSENSUS_WEIGHT}
        )
        cf_row = next(
            row
            for row in rows
            if row.coordinate.output == LendingOutput.COLLATERAL_FACTOR.value
        )
        assert cf_row.value == Decimal(6000)


class TestWeightedMedianDegenerateWeights:
    def test_all_zero_weights_returns_positional_median_not_minimum(self) -> None:
        # Guard the zero-total-weight path: it must not collapse to the minimum
        # sample (the old behavior), which would silently pick the lowest bid.
        values = [(5000, Decimal(0)), (5200, Decimal(0)), (6000, Decimal(0))]

        assert weighted_median(values) == Decimal(5200)
