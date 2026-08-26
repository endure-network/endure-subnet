"""Alpha Risk tier derivation (risk scope spec §Derived risk tier)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

RiskTier = Literal["A", "B", "C", "D", "E", "unrated"]

# Frozen at R6 flip (2026-07-07); changes require a versioned spec update.
TIER_A_MAX_DRAWDOWN_BPS: Final = Decimal(1000)
TIER_A_MAX_VOLATILITY_BPS: Final = Decimal(5000)
TIER_B_MAX_DRAWDOWN_BPS: Final = Decimal(2000)
TIER_B_MAX_VOLATILITY_BPS: Final = Decimal(8000)
TIER_C_MAX_DRAWDOWN_BPS: Final = Decimal(3500)
TIER_C_MAX_VOLATILITY_BPS: Final = Decimal(12000)
TIER_D_MAX_DRAWDOWN_BPS: Final = Decimal(5000)
TIER_D_MAX_VOLATILITY_BPS: Final = Decimal(16000)


@dataclass(frozen=True, slots=True)
class RiskTierInputs:
    max_drawdown_bps: Decimal | None
    realized_volatility_bps: Decimal | None
    max_drawdown_voided: bool = False
    realized_volatility_voided: bool = False


def derive_risk_tier(inputs: RiskTierInputs) -> RiskTier:
    if (
        inputs.max_drawdown_bps is None
        or inputs.realized_volatility_bps is None
        or inputs.max_drawdown_voided
        or inputs.realized_volatility_voided
    ):
        return "unrated"
    drawdown = inputs.max_drawdown_bps
    volatility = inputs.realized_volatility_bps
    if drawdown < TIER_A_MAX_DRAWDOWN_BPS and volatility < TIER_A_MAX_VOLATILITY_BPS:
        return "A"
    if drawdown < TIER_B_MAX_DRAWDOWN_BPS and volatility < TIER_B_MAX_VOLATILITY_BPS:
        return "B"
    if drawdown < TIER_C_MAX_DRAWDOWN_BPS and volatility < TIER_C_MAX_VOLATILITY_BPS:
        return "C"
    if drawdown < TIER_D_MAX_DRAWDOWN_BPS and volatility < TIER_D_MAX_VOLATILITY_BPS:
        return "D"
    return "E"
