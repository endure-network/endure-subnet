"""Realized-optimal observables for lending outputs (Forge MVP v2 spec §8.1.2)."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from endure.scoring.context import TR_CONTEXT

MIN_COLLATERAL_FACTOR_PRICE_POINTS = 2


def collateral_factor_optimal_bps(
    prices: Sequence[Decimal], *, liquidation_buffer_bps: int
) -> int:
    """Realized-optimal collateral factor in LTV bps over a forward window.

    ``prices[0]`` is the submission entry price and later points are the forward
    scoring window. The target is the worst retained value fraction from entry,
    less liquidation headroom, floored at 0.
    """
    if len(prices) < MIN_COLLATERAL_FACTOR_PRICE_POINTS:
        raise ValueError("prices must include entry and at least one forward point")
    if liquidation_buffer_bps < 0:
        raise ValueError("liquidation_buffer_bps must be non-negative")
    with localcontext(TR_CONTEXT):
        entry = prices[0]
        if entry <= 0 or any(price <= 0 for price in prices):
            raise ValueError("prices must be positive")
        retained_bps = ((min(prices) / entry) * Decimal(10000)).quantize(
            Decimal("1"), rounding=ROUND_HALF_EVEN
        )
        return max(0, int(retained_bps) - liquidation_buffer_bps)
