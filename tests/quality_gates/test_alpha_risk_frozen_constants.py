from __future__ import annotations

from decimal import Decimal

from endure.assessment.schemas.subnet_alpha_risk import (
    HORIZON_5D_SECONDS,
    HORIZON_30D_SECONDS,
    RISK_SPECS_BY_OUTPUT,
    RiskOutput,
)
from endure.assessment.schemas.wire import AggressiveDirection, DeviationMode
from endure.protocol.risk_miner import BASELINE_BPS_VALUES
from endure.publication import risk_tier
from endure.scoring.risk import observables


def test_alpha_risk_scoring_constants_are_frozen_at_r6_flip() -> None:
    # Changes now require a versioned spec update (risk scope §Scoring, R6 flip).
    assert HORIZON_5D_SECONDS == 432_000
    assert HORIZON_30D_SECONDS == 2_592_000
    assert {
        output: (
            spec.grace_band,
            spec.cutoff,
            spec.aggressive_direction,
            spec.deviation_mode,
            spec.lenient_multiplier,
        )
        for output, spec in RISK_SPECS_BY_OUTPUT.items()
    } == {
        RiskOutput.MAX_DRAWDOWN: (
            200,
            2000,
            AggressiveDirection.LOWER,
            DeviationMode.ABSOLUTE,
            Decimal("3"),
        ),
        RiskOutput.REALIZED_VOLATILITY: (
            500,
            5000,
            AggressiveDirection.LOWER,
            DeviationMode.ABSOLUTE,
            Decimal("3"),
        ),
        RiskOutput.TWAP_PRICE: (
            200,
            2000,
            AggressiveDirection.HIGHER,
            DeviationMode.RELATIVE,
            Decimal("3"),
        ),
        RiskOutput.LIQUIDITY_DEPTH: (
            500,
            5000,
            AggressiveDirection.HIGHER,
            DeviationMode.RELATIVE,
            Decimal("3"),
        ),
    }


def test_alpha_risk_runtime_constants_are_frozen_at_r6_flip() -> None:
    # Changes now require a versioned spec update (risk scope §Derived risk tier).
    assert BASELINE_BPS_VALUES == {
        (RiskOutput.MAX_DRAWDOWN, HORIZON_5D_SECONDS): 1500,
        (RiskOutput.MAX_DRAWDOWN, HORIZON_30D_SECONDS): 3000,
        (RiskOutput.REALIZED_VOLATILITY, HORIZON_5D_SECONDS): 8000,
        (RiskOutput.REALIZED_VOLATILITY, HORIZON_30D_SECONDS): 8000,
    }
    assert observables.CANONICAL_ALPHA_SNAPSHOT_CADENCE_SECONDS == 7_200
    assert observables.CANONICAL_ALPHA_SNAPSHOT_CADENCE_BLOCKS == 600
    assert observables.MIN_REALIZED_WINDOW_SNAPSHOTS == 20
    assert observables.MIN_VOLATILITY_EXACT_CADENCE_RETURNS == 10
    assert observables.MIN_REALIZED_WINDOW_COVERAGE_NUMERATOR == 4
    assert observables.MIN_REALIZED_WINDOW_COVERAGE_DENOMINATOR == 5


def test_alpha_risk_tier_table_is_frozen_at_r6_flip() -> None:
    # Changes now require a versioned spec update (risk scope §Derived risk tier).
    assert (
        risk_tier.TIER_A_MAX_DRAWDOWN_BPS,
        risk_tier.TIER_A_MAX_VOLATILITY_BPS,
        risk_tier.TIER_B_MAX_DRAWDOWN_BPS,
        risk_tier.TIER_B_MAX_VOLATILITY_BPS,
        risk_tier.TIER_C_MAX_DRAWDOWN_BPS,
        risk_tier.TIER_C_MAX_VOLATILITY_BPS,
        risk_tier.TIER_D_MAX_DRAWDOWN_BPS,
        risk_tier.TIER_D_MAX_VOLATILITY_BPS,
    ) == (
        Decimal(1000),
        Decimal(5000),
        Decimal(2000),
        Decimal(8000),
        Decimal(3500),
        Decimal(12000),
        Decimal(5000),
        Decimal(16000),
    )
