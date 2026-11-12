"""Shared deterministic EMA and score-to-weight transformations (risk scope spec §Scoring)."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext

from endure.assessment.coordinates import AssessmentEmaState
from endure.scoring.context import TR_CONTEXT
from endure.scoring.policy import LONG_HORIZON_HOLD_BACK, WEIGHT_SHARPENING_GAMMA


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


def ema_maturity(resolved_rounds: int, *, half_life_rounds: int) -> Decimal:
    """The largest value a zero-prior EMA can reach after ``resolved_rounds``."""
    if half_life_rounds <= 0:
        raise ValueError("half_life_rounds must be positive")
    with localcontext(TR_CONTEXT):
        return Decimal(1) - Decimal(2) ** (
            Decimal(-resolved_rounds) / Decimal(half_life_rounds)
        )


def blended_score(
    states: Sequence[AssessmentEmaState],
    *,
    horizons: Sequence[int],
    half_life_rounds: int,
) -> Decimal:
    """One miner's score across its coordinate EMAs.

    With one horizon this is the mean EMA. With several, each (target, output)
    pair adds its shortest and longest EMAs and divides by 1 plus the longest's
    maturity, so a young EMA does not read as poor accuracy, then holds back a
    share until the longest has a record. A zero observation on either horizon
    cannot raise the score.
    """
    if not states:
        return Decimal(0)
    with localcontext(TR_CONTEXT):
        if len(horizons) <= 1:
            total = sum((state.ema for state in states), Decimal(0))
            return total / Decimal(len(states))
        short, long = min(horizons), max(horizons)
        pairs: dict[tuple[str, str, str], dict[int, AssessmentEmaState]] = {}
        for state in states:
            coordinate = state.coordinate
            key = (coordinate.target_kind, coordinate.target_id, coordinate.output)
            pairs.setdefault(key, {})[coordinate.horizon_value] = state
        maturity = {
            rounds: ema_maturity(rounds, half_life_rounds=half_life_rounds)
            for rounds in {
                state.resolved_rounds
                for state in states
                if state.coordinate.horizon_value == long
            }
        }
        total = Decimal(0)
        for key in sorted(pairs):
            rows = pairs[key]
            earned = sum((rows[h].ema for h in (short, long) if h in rows), Decimal(0))
            record = (
                maturity[rows[long].resolved_rounds] if long in rows else Decimal(0)
            )
            total += earned * (1 - LONG_HORIZON_HOLD_BACK * (1 - record)) / (1 + record)
        return total / Decimal(len(pairs))


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
