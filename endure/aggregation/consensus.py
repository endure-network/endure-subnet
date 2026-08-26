"""Weighted-median consensus aggregation primitives (spec §9).

Computed once at embargo lift and stored — never recomputed, never served
while a round is open. Each coordinate gets the weighted median of miner
submissions, weighted by blended accuracy EMA with a floor so unproven miners
still contribute epsilon (otherwise there is no consensus in week one), plus a
weighted-MAD dispersion band — the confidence analog.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, localcontext

from endure.scoring.context import TR_CONTEXT

# Epsilon weight for miners without resolved history. [replay]
MIN_CONSENSUS_WEIGHT = Decimal("0.01")


def weighted_median(values: list[tuple[int, Decimal]]) -> Decimal:
    """Median of integer values under Decimal weights; deterministic on ties."""
    if not values:
        raise ValueError("weighted median of nothing")
    ordered = sorted(values)
    with localcontext(TR_CONTEXT):
        total = sum((weight for _, weight in ordered), Decimal(0))
        if total <= 0:
            # Degenerate all-zero (or non-positive) weights: fall back to the
            # unweighted positional median so consensus does not silently
            # collapse to the minimum sample.
            return Decimal(ordered[(len(ordered) - 1) // 2][0])
        half = total / 2
        cumulative = Decimal(0)
        for value, weight in ordered:
            cumulative += weight
            if cumulative >= half:
                return Decimal(value)
    return Decimal(ordered[-1][0])


def _weighted_mad(values: list[tuple[int, Decimal]], median: Decimal) -> Decimal:
    # weighted_median always returns one integral sample, never an average, so
    # the median is integer-valued and every deviation stays a pure int — no
    # Decimal round-trip per value.
    median_value = int(median)
    deviations = [(abs(value - median_value), weight) for value, weight in values]
    return weighted_median(deviations)


def consensus_weight(blends: Mapping[str, Decimal], hotkey: str) -> Decimal:
    """Blend weight for one hotkey, floored so unproven miners still count."""
    return max(blends.get(hotkey, Decimal(0)), MIN_CONSENSUS_WEIGHT)


def aggregate_cell(entries: list[tuple[int, Decimal]]) -> tuple[Decimal, Decimal, int]:
    """(weighted median, weighted-MAD dispersion, sample count) for one cell.

    The single consensus policy for a bucketed cell — every schema's consensus
    must aggregate through this so the semantics cannot drift between schemas.
    """
    median = weighted_median(entries)
    return median, _weighted_mad(entries, median), len(entries)
