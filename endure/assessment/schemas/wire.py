"""Shared assessment wire helpers (risk scope spec §R1)."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Generic, LiteralString, Protocol, TypeVar

from pydantic_core import PydanticCustomError

# Structured pydantic error type every schema raises out of bounds, so generic
# validation classifies one code instead of branching per schema.
OUT_OF_BOUNDS_ERROR = "endure_out_of_bounds"


class OutputKey(Protocol):
    @property
    def value(self) -> str: ...


OutputT = TypeVar("OutputT", bound=OutputKey, covariant=True)


class AggressiveDirection(StrEnum):
    """Which side of the target is the dangerous one (penalized harder)."""

    HIGHER = "higher"
    LOWER = "lower"


class DeviationMode(StrEnum):
    """How a deviation is measured before the bands apply."""

    ABSOLUTE = "absolute"
    RELATIVE = "relative"


class WireParameterSpec(Protocol[OutputT]):
    @property
    def output(self) -> OutputT: ...

    @property
    def confidence_floor_bps(self) -> int: ...

    @property
    def min_value(self) -> int: ...

    @property
    def max_value(self) -> int | None: ...

    @property
    def unit(self) -> str: ...


class CanonicalOutput(Protocol):
    @property
    def output(self) -> OutputKey: ...

    @property
    def horizon_seconds(self) -> int: ...

    def model_dump(self, *, mode: str) -> dict[str, object]: ...


class CanonicalAsset(Protocol):
    @property
    def netuid(self) -> int: ...

    @property
    def outputs(self) -> tuple[CanonicalOutput, ...]: ...


@dataclass(frozen=True, slots=True)
class ReasonCodePolicy:
    max_codes: int
    max_length: int
    pattern: re.Pattern[str]


@dataclass(frozen=True, slots=True)
class ParameterBounds:
    scoring_weight_positive: bool
    grace_band: int
    cutoff: int
    lenient_multiplier_at_least_one: bool
    confidence_floor_bps: int
    max_confidence_bps: int
    min_value: int
    max_value: int | None


@dataclass(frozen=True, slots=True)
class OutputWireValue(Generic[OutputT]):
    output: OutputT
    value: int
    confidence_bps: int
    unit: str


@dataclass(frozen=True, slots=True)
class BaseParameterSpec(Generic[OutputT]):
    output: OutputT
    scoring_weight: Decimal
    grace_band: int
    cutoff: int
    aggressive_direction: AggressiveDirection
    lenient_multiplier: Decimal
    deviation_mode: DeviationMode
    unit: str
    confidence_floor_bps: int
    scored_live: bool
    min_value: int
    max_value: int | None

    def __post_init__(self) -> None:
        validate_parameter_bounds(
            ParameterBounds(
                scoring_weight_positive=self.scoring_weight > 0,
                grace_band=self.grace_band,
                cutoff=self.cutoff,
                lenient_multiplier_at_least_one=self.lenient_multiplier >= 1,
                confidence_floor_bps=self.confidence_floor_bps,
                max_confidence_bps=10000,
                min_value=self.min_value,
                max_value=self.max_value,
            )
        )


def validate_parameter_bounds(bounds: ParameterBounds) -> None:
    """Validate shared scoring-spec invariants for assessment schemas."""
    if not bounds.scoring_weight_positive:
        raise ValueError("scoring_weight must be positive")
    if bounds.grace_band < 0:
        raise ValueError("grace_band must be non-negative")
    if bounds.cutoff <= bounds.grace_band:
        raise ValueError("cutoff must be greater than grace_band")
    if not bounds.lenient_multiplier_at_least_one:
        raise ValueError("lenient_multiplier must be >= 1")
    if not 0 <= bounds.confidence_floor_bps <= bounds.max_confidence_bps:
        raise ValueError(
            f"confidence_floor_bps must be inside [0, {bounds.max_confidence_bps}]"
        )
    if bounds.min_value < 0:
        raise ValueError("min_value must be non-negative")
    if bounds.max_value is not None and bounds.max_value < bounds.min_value:
        raise ValueError("max_value must be >= min_value")


def validate_confidence_bps(
    value: int, *, upper: int, error_code: LiteralString
) -> int:
    if not 0 <= value <= upper:
        raise PydanticCustomError(
            error_code,
            "confidence_bps={value} outside [0, {upper}]",
            {"value": value, "upper": upper},
        )
    return value


def validate_reason_codes(
    value: tuple[str, ...], *, policy: ReasonCodePolicy
) -> tuple[str, ...]:
    if len(value) > policy.max_codes:
        raise ValueError(f"too many reason_codes: {len(value)} > {policy.max_codes}")
    if len(set(value)) != len(value):
        raise ValueError("duplicate reason_codes are not allowed")
    for reason_code in value:
        if len(reason_code) > policy.max_length:
            raise ValueError(f"reason_code exceeds {policy.max_length} characters")
        if not policy.pattern.fullmatch(reason_code):
            raise ValueError(f"invalid reason_code: {reason_code}")
    return tuple(sorted(value))


def validate_output_value_against_spec(
    *,
    submitted: OutputWireValue[OutputT],
    spec: WireParameterSpec[OutputT],
    error_code: LiteralString,
) -> None:
    if submitted.unit != spec.unit:
        raise ValueError(
            f"unit for {submitted.output.value} must be {spec.unit}, got {submitted.unit}"
        )
    if submitted.confidence_bps < spec.confidence_floor_bps:
        raise PydanticCustomError(
            error_code,
            "confidence_bps={value} below {output} floor {floor}",
            {
                "value": submitted.confidence_bps,
                "output": submitted.output.value,
                "floor": spec.confidence_floor_bps,
            },
        )
    if submitted.value < spec.min_value:
        raise PydanticCustomError(
            error_code,
            "{output}={value} below minimum {minimum}",
            {
                "output": submitted.output.value,
                "value": submitted.value,
                "minimum": spec.min_value,
            },
        )
    if spec.max_value is not None and submitted.value > spec.max_value:
        raise PydanticCustomError(
            error_code,
            "{output}={value} above maximum {maximum}",
            {
                "output": submitted.output.value,
                "value": submitted.value,
                "maximum": spec.max_value,
            },
        )


def validate_unique_netuids(netuids: Sequence[int]) -> None:
    if len(set(netuids)) != len(netuids):
        raise ValueError("duplicate netuid in submission")


def canonical_assets_payload(
    assets: Sequence[CanonicalAsset],
    *,
    output_key: Callable[[CanonicalOutput], tuple[int, str]],
) -> list[dict[str, object]]:
    ordered_assets = sorted(assets, key=lambda asset: asset.netuid)
    return [
        {
            "netuid": asset.netuid,
            "outputs": [
                output.model_dump(mode="json")
                for output in sorted(asset.outputs, key=output_key)
            ],
        }
        for asset in ordered_assets
    ]
