"""Shared deterministic EMA and score-to-weight transformations (risk scope spec §Scoring)."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, localcontext

from endure.scoring.context import TR_CONTEXT
from endure.scoring.policy import WEIGHT_SHARPENING_GAMMA


def ema_update(
    previous: Decimal | None,
    observation: Decimal,
    *,
    half_life_rounds: int,
) -> Decimal:
    """Update payout-memory EMA with a zero prior for new miners."""
    if half_life_rounds <= 0:
        raise ValueError("half_life_rounds must be positive")
    if previous is None:
        previous = Decimal(0)
    with localcontext(TR_CONTEXT):
        alpha = Decimal(1) - Decimal(2) ** (Decimal(-1) / Decimal(half_life_rounds))
        return alpha * observation + (Decimal(1) - alpha) * previous


def normalize_weights(
    scores: Mapping[str, Decimal],
    *,
    gamma: int = WEIGHT_SHARPENING_GAMMA,
) -> dict[str, Decimal]:
    """Normalize sharpened scores into deterministic emission weights."""
    with localcontext(TR_CONTEXT):
        powered = {key: scores[key] ** gamma for key in scores}
        total = Decimal(0)
        for key in sorted(powered):
            total += powered[key]
        if total == 0:
            return {key: Decimal(0) for key in scores}
        return {key: value / total for key, value in powered.items()}
