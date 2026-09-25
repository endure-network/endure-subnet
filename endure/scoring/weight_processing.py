"""Deterministic score-to-chain weight math with explicit chain constraints."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext

from endure.scoring.context import TR_CONTEXT

U16_MAX = 65535

ZERO = Decimal("0")
ONE = Decimal("1")
EPSILON = Decimal("1e-7")
MIN_NON_ZERO_WEIGHT = Decimal("1e-5")
U16_MAX_DECIMAL = Decimal(U16_MAX)


def coerce_decimal(value: object) -> Decimal:
    candidate = value if isinstance(value, Decimal) else Decimal(str(value))
    if candidate.is_nan():
        return ZERO
    return candidate


def _as_list(values: Sequence[object] | object) -> list[object]:
    tolist = getattr(values, "tolist", None)
    if callable(tolist):
        converted = tolist()
        return converted if isinstance(converted, list) else [converted]
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
        return list(values)
    return [values]


def _as_decimal_list(values: Sequence[object] | object) -> list[Decimal]:
    return [coerce_decimal(value) for value in _as_list(values)]


def _as_uid_list(uids: Sequence[object] | object) -> list[int]:
    return [int(str(uid)) for uid in _as_list(uids)]


def normalize_scores(scores: Sequence[Decimal]) -> list[Decimal]:
    """Clamp negative scores before normalization; all nonpositive means abstain."""
    with localcontext(TR_CONTEXT):
        clamped = [score if score > ZERO else ZERO for score in scores]
        norm = sum(clamped, ZERO)
        if norm <= ZERO:
            return [ZERO] * len(scores)
        return [score / norm for score in clamped]


def _cumulative_sum(values: Sequence[Decimal]) -> list[Decimal]:
    total = ZERO
    cumulative: list[Decimal] = []
    for value in values:
        total += value
        cumulative.append(total)
    return cumulative


def _normalize_to_unit(weights: Sequence[Decimal]) -> list[Decimal]:
    if not weights:
        return []

    total = sum(weights, ZERO)
    if total == ZERO:
        uniform = ONE / Decimal(len(weights))
        normalized = [uniform] * len(weights)
    else:
        normalized = [weight / total for weight in weights]

    correction = ONE - sum(normalized, ZERO)
    normalized[-1] += correction
    return normalized


def _decimal_quantile(values: Sequence[Decimal], q: Decimal) -> Decimal:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("quantile requires at least one value")
    if len(ordered) == 1:
        return ordered[0]

    position = q * Decimal(len(ordered) - 1)
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    fraction = position - Decimal(lower_index)

    lower = ordered[lower_index]
    upper = ordered[upper_index]
    return lower + (upper - lower) * fraction


def normalize_max_weight(
    x: Sequence[object],
    limit: Decimal | str = Decimal("0.1"),
) -> list[Decimal]:
    """Normalize and cap weights, or use uniform weights if the cap is infeasible."""
    with localcontext(TR_CONTEXT):
        weights = _as_decimal_list(x)
        if not weights:
            return []

        limit_decimal = coerce_decimal(limit)
        total = sum(weights, ZERO)
        if total == ZERO or (Decimal(len(weights)) * limit_decimal) <= ONE:
            uniform = ONE / Decimal(len(weights))
            return [uniform] * len(weights)

        values = sorted(weights)
        value_sum = sum(values, ZERO)
        estimation = [value / value_sum for value in values]

        if max(estimation) <= limit_decimal:
            return _normalize_to_unit(weights)

        cumsum = _cumulative_sum(estimation)
        estimation_sum = [
            Decimal(len(values) - i - 1) * estimation[i] for i in range(len(values))
        ]
        n_values = sum(
            1
            for estimate, estimate_total, cumulative in zip(
                estimation, estimation_sum, cumsum, strict=True
            )
            if estimate / (estimate_total + cumulative + EPSILON) < limit_decimal
        )

        if n_values == 0:
            return _normalize_to_unit(weights)

        denominator = ONE - (limit_decimal * Decimal(len(estimation) - n_values))
        if denominator <= ZERO:
            return _normalize_to_unit(weights)

        cutoff_scale = (limit_decimal * cumsum[n_values - 1] - EPSILON) / denominator
        cutoff = cutoff_scale * value_sum

        capped_weights = [min(weight, cutoff) for weight in weights]
        if sum(capped_weights, ZERO) == ZERO:
            uniform = ONE / Decimal(len(capped_weights))
            return [uniform] * len(capped_weights)

        return _normalize_to_unit(capped_weights)


def convert_weights_and_uids_for_emit(
    uids: Sequence[object],
    weights: Sequence[object],
) -> tuple[list[int], list[int]]:
    """Scale Decimal weights to u16, round half up, and omit rounded zeros."""
    with localcontext(TR_CONTEXT):
        uid_list = _as_uid_list(uids)
        weight_list = _as_decimal_list(weights)

        if len(uid_list) != len(weight_list):
            raise ValueError(
                "Passed weights and uids must have the same length, got {} and {}".format(
                    len(uid_list), len(weight_list)
                )
            )

        if any(weight < ZERO for weight in weight_list):
            raise ValueError(
                "Passed weight is negative cannot exist on chain {}".format(weight_list)
            )
        if any(uid < 0 for uid in uid_list):
            raise ValueError(
                "Passed uid is negative cannot exist on chain {}".format(uid_list)
            )

        total = sum(weight_list, ZERO)
        if total == ZERO:
            return [], []

        max_weight = max(weight_list)
        scaled_weights = [weight / max_weight for weight in weight_list]

        weight_vals: list[int] = []
        weight_uids: list[int] = []
        for weight_i, uid_i in zip(scaled_weights, uid_list, strict=True):
            uint16_val = int(
                (weight_i * U16_MAX_DECIMAL).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
            if uint16_val != 0:
                weight_vals.append(uint16_val)
                weight_uids.append(uid_i)
        return weight_uids, weight_vals


def process_weights(
    uids: Sequence[object],
    weights: Sequence[object],
    *,
    metagraph_size: int,
    min_allowed_weights: int,
    max_weight_limit: Decimal,
    exclude_quantile: int = 0,
) -> tuple[list[int], list[Decimal]]:
    """Apply chain constraints to an explicit UID/weight mapping without SDK access.

    Metagraph UIDs occupy ``range(metagraph_size)``. Sparse or reordered input
    retains its UID mapping; floor padding expands it to that complete range.
    """
    with localcontext(TR_CONTEXT):
        uid_list = _as_uid_list(uids)
        weight_list = _as_decimal_list(weights)

        if len(uid_list) != len(weight_list):
            raise ValueError(
                "Passed weights and uids must have the same length, got {} and {}".format(
                    len(uid_list), len(weight_list)
                )
            )

        quantile = Decimal(exclude_quantile) / U16_MAX_DECIMAL
        if any(uid < 0 or uid >= metagraph_size for uid in uid_list):
            raise ValueError(
                f"Passed uid outside metagraph range [0, {metagraph_size}): {uid_list}"
            )
        if metagraph_size == 0:
            return [], []

        non_zero_pairs = [
            (uid, weight)
            for uid, weight in zip(uid_list, weight_list, strict=True)
            if weight > ZERO
        ]

        if len(non_zero_pairs) == 0 or metagraph_size < min_allowed_weights:
            uniform = ONE / Decimal(metagraph_size)
            final_weights = [uniform] * metagraph_size
            return list(range(metagraph_size)), final_weights

        if len(non_zero_pairs) < min_allowed_weights:
            padded_weights = [MIN_NON_ZERO_WEIGHT] * metagraph_size
            for uid, weight in non_zero_pairs:
                padded_weights[uid] += weight
            normalized_weights = normalize_max_weight(
                x=padded_weights,
                limit=max_weight_limit,
            )
            return list(range(metagraph_size)), normalized_weights

        non_zero_weight_uids = [uid for uid, _weight in non_zero_pairs]
        non_zero_weights = [weight for _uid, weight in non_zero_pairs]

        max_exclude = max(
            ZERO,
            Decimal(len(non_zero_weights) - min_allowed_weights)
            / Decimal(len(non_zero_weights)),
        )
        exclude_quantile_decimal = min(quantile, max_exclude)
        lowest_quantile = _decimal_quantile(non_zero_weights, exclude_quantile_decimal)

        filtered_pairs = [
            (uid, weight)
            for uid, weight in zip(non_zero_weight_uids, non_zero_weights, strict=True)
            if lowest_quantile <= weight
        ]
        filtered_uids = [uid for uid, _weight in filtered_pairs]
        filtered_weights = [weight for _uid, weight in filtered_pairs]

        normalized_weights = normalize_max_weight(
            x=filtered_weights,
            limit=max_weight_limit,
        )
        return filtered_uids, normalized_weights


# Protocol constant: no low-weight quantile exclusion before chain limits.
EMISSION_EXCLUDE_QUANTILE = 0


def emission_candidate(scores: Sequence[Decimal]) -> list[Decimal] | None:
    """Normalized raw weights, or ``None`` to abstain when no score is positive.

    All-zero input must never reach ``process_weights``' uniform branch: that
    would emit noise instead of abstaining.
    """
    raw_weights = normalize_scores(scores)
    if not any(weight > ZERO for weight in raw_weights):
        return None
    return raw_weights


@dataclass(frozen=True, slots=True)
class ChainWeightVector:
    """Chain-constrained processed weights and their u16 submission form."""

    processed_uids: tuple[int, ...]
    processed_weights: tuple[Decimal, ...]
    uint_uids: tuple[int, ...]
    uint_weights: tuple[int, ...]


def chain_weight_vector(
    raw_weights: Sequence[Decimal],
    *,
    uids: Sequence[object],
    metagraph_size: int,
    min_allowed_weights: int,
    max_weight_limit: Decimal,
) -> ChainWeightVector:
    """Apply chain limits to an emission candidate, then encode it as u16."""
    processed_uids, processed_weights = process_weights(
        uids=uids,
        weights=raw_weights,
        metagraph_size=metagraph_size,
        min_allowed_weights=min_allowed_weights,
        max_weight_limit=max_weight_limit,
        exclude_quantile=EMISSION_EXCLUDE_QUANTILE,
    )
    uint_uids, uint_weights = convert_weights_and_uids_for_emit(
        uids=processed_uids, weights=processed_weights
    )
    return ChainWeightVector(
        processed_uids=tuple(processed_uids),
        processed_weights=tuple(processed_weights),
        uint_uids=tuple(uint_uids),
        uint_weights=tuple(uint_weights),
    )
