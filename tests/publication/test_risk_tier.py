"""Risk tier derivation (risk scope spec §Derived risk tier)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from endure.publication.risk_tier import RiskTierInputs, derive_risk_tier


@pytest.mark.parametrize(
    ("drawdown", "volatility", "tier"),
    [
        (999, 4999, "A"),
        (1000, 4999, "B"),
        (999, 5000, "B"),
        (1999, 7999, "B"),
        (2000, 7999, "C"),
        (1999, 8000, "C"),
        (3499, 11999, "C"),
        (3500, 11999, "D"),
        (3499, 12000, "D"),
        (4999, 15999, "D"),
        (5000, 15999, "E"),
        (4999, 16000, "E"),
    ],
)
def test_tier_edges_are_strict_less_than_thresholds(
    drawdown: int, volatility: int, tier: str
) -> None:
    inputs = RiskTierInputs(
        max_drawdown_bps=Decimal(drawdown),
        realized_volatility_bps=Decimal(volatility),
    )

    assert derive_risk_tier(inputs) == tier


@pytest.mark.parametrize(
    ("drawdown", "volatility", "tier"),
    [
        (999, 15999, "D"),
        (4999, 4999, "D"),
        (100, 16000, "E"),
        (5000, 100, "E"),
    ],
)
def test_worse_dimension_wins(drawdown: int, volatility: int, tier: str) -> None:
    inputs = RiskTierInputs(
        max_drawdown_bps=Decimal(drawdown),
        realized_volatility_bps=Decimal(volatility),
    )

    assert derive_risk_tier(inputs) == tier


@pytest.mark.parametrize(
    "inputs",
    [
        RiskTierInputs(max_drawdown_bps=None, realized_volatility_bps=Decimal(100)),
        RiskTierInputs(max_drawdown_bps=Decimal(100), realized_volatility_bps=None),
        RiskTierInputs(
            max_drawdown_bps=Decimal(100),
            realized_volatility_bps=Decimal(100),
            max_drawdown_voided=True,
        ),
        RiskTierInputs(
            max_drawdown_bps=Decimal(100),
            realized_volatility_bps=Decimal(100),
            realized_volatility_voided=True,
        ),
    ],
)
def test_unrated_when_thirty_day_consensus_missing_or_voided(
    inputs: RiskTierInputs,
) -> None:
    assert derive_risk_tier(inputs) == "unrated"
