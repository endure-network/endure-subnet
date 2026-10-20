"""Shared deterministic EMA and score-to-weight transformations (risk scope spec §Scoring)."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, localcontext

from endure.scoring.context import TR_CONTEXT
from endure.scoring.policy import WEIGHT_SHARPENING_GAMMA


@dataclass(frozen=True, slots=True)
class Payout:
    """A validator's newest earned weights and each pooled hotkey's pool size.

    The weights are pooled, before the vote withholds owner-coldkey hotkeys
    and before the owner burn.
    """

    weights: Mapping[str, Decimal]
    pool_sizes: Mapping[str, int]


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
    pools: Iterable[Collection[str]] = (),
) -> dict[str, Decimal]:
    """Normalize sharpened scores into deterministic emission weights.

    Each identical-forecast pool in ``pools`` weighs what its best member
    would alone: every member gives up the same amount of sharpened score, or
    all of its own if that is less. Equal members split one hotkey's weight,
    and members that join a pool never cost the others more, in total, than
    they give up themselves.
    """
    with localcontext(TR_CONTEXT):
        powered = {key: scores[key] ** gamma for key in scores}
        for pool in pools:
            members = {key: powered[key] for key in pool if key in powered}
            powered.update(_level_to_best(members))
        total = Decimal(0)
        for key in sorted(powered):
            total += powered[key]
        if total == 0:
            return {key: Decimal(0) for key in scores}
        return {key: value / total for key, value in powered.items()}


def _level_to_best(members: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """Cut every member by one level so the pool keeps its largest weight.

    The level is the single cut, never taking a member below zero, that
    leaves the members together exactly the largest weight among them.
    """
    ordered = sorted(members.values(), reverse=True)
    level = Decimal(0)
    total = Decimal(0)
    for count, value in enumerate(ordered, start=1):
        total += value
        candidate = (total - ordered[0]) / count
        if value <= candidate:
            break
        level = candidate
    return {key: max(value - level, Decimal(0)) for key, value in members.items()}
