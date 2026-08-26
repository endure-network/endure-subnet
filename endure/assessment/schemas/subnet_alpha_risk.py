"""Alpha Risk V1 schema (risk scope spec §V1 scored schema).

Miners submit four per-subnet Alpha risk observables at both V1 horizons. The
envelope mirrors the dormant Forge lending bundle while every output is scored
live from day one per risk scope §Scoring.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictInt, field_validator, model_validator

from endure.assessment.schemas.wire import (
    OUT_OF_BOUNDS_ERROR as OUT_OF_BOUNDS_ERROR,
)
from endure.assessment.schemas.wire import (
    AggressiveDirection,
    BaseParameterSpec,
    DeviationMode,
    OutputWireValue,
    ReasonCodePolicy,
    canonical_assets_payload,
    validate_confidence_bps,
    validate_output_value_against_spec,
    validate_reason_codes,
    validate_unique_netuids,
)

RISK_SCHEMA_ID = "risk.v1.subnet_alpha"
# risk scope §Locked V1 decisions: both horizons are scored from day one.
HORIZON_5D_SECONDS = 5 * 24 * 60 * 60
HORIZON_30D_SECONDS = 30 * 24 * 60 * 60
RISK_HORIZONS = (HORIZON_5D_SECONDS, HORIZON_30D_SECONDS)
MAX_RISK_ASSETS_PER_BUNDLE = 256
MAX_REASON_CODES_PER_OUTPUT = 8
MAX_REASON_CODE_LENGTH = 64
REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
REASON_CODE_POLICY = ReasonCodePolicy(
    max_codes=MAX_REASON_CODES_PER_OUTPUT,
    max_length=MAX_REASON_CODE_LENGTH,
    pattern=REASON_CODE_PATTERN,
)
MAX_CONFIDENCE_BPS = 10000
DEFAULT_CONFIDENCE_FLOOR_BPS = 1000
RISK_VALUE_MAX_INT64 = 2**63 - 1


@dataclass(frozen=True, slots=True)
class SubnetAlphaTarget:
    """Axis 1 - a Bittensor subnet Alpha token, identified by netuid."""

    netuid: int

    def __post_init__(self) -> None:
        if self.netuid < 0:
            raise ValueError(f"netuid must be non-negative: {self.netuid}")


@dataclass(frozen=True, slots=True)
class RiskContextV1:
    """Axis 2 - Alpha Risk V1 observable recommendation."""

    context_id: str = "risk.v1"


class RiskOutput(StrEnum):
    """The four Alpha Risk V1 output wire names."""

    MAX_DRAWDOWN = "max_drawdown"
    REALIZED_VOLATILITY = "realized_volatility"
    TWAP_PRICE = "twap_price"
    LIQUIDITY_DEPTH = "liquidity_depth"


@dataclass(frozen=True, slots=True)
class RiskParameterSpec(BaseParameterSpec[RiskOutput]):
    """One scored Alpha Risk output and its asymmetric deviation bands."""


@dataclass(frozen=True, slots=True)
class RiskSchema:
    """Axis 3 - the Alpha Risk output bundle."""

    schema_id: str
    target_type: type[SubnetAlphaTarget]
    context: RiskContextV1
    parameters: tuple[RiskParameterSpec, ...]


# risk scope §Scoring and §V1 scored schema. Bounds are intentionally generous:
# drawdown is a percent capped at 10000 bps; annualized volatility allows 100x;
# RAO price/depth wire values are bounded to signed int64 for cross-language
# safety, with price strictly positive and depth allowing an empty pool reserve.
MAX_DRAWDOWN_SPEC = RiskParameterSpec(
    output=RiskOutput.MAX_DRAWDOWN,
    scoring_weight=Decimal("1.0"),
    grace_band=200,
    cutoff=2000,
    aggressive_direction=AggressiveDirection.LOWER,
    lenient_multiplier=Decimal("3"),
    deviation_mode=DeviationMode.ABSOLUTE,
    unit="bps",
    confidence_floor_bps=DEFAULT_CONFIDENCE_FLOOR_BPS,
    scored_live=True,
    min_value=0,
    max_value=10000,
)
REALIZED_VOLATILITY_SPEC = RiskParameterSpec(
    output=RiskOutput.REALIZED_VOLATILITY,
    scoring_weight=Decimal("1.0"),
    grace_band=500,
    cutoff=5000,
    aggressive_direction=AggressiveDirection.LOWER,
    lenient_multiplier=Decimal("3"),
    deviation_mode=DeviationMode.ABSOLUTE,
    unit="bps",
    confidence_floor_bps=DEFAULT_CONFIDENCE_FLOOR_BPS,
    scored_live=True,
    min_value=0,
    max_value=1_000_000,
)
TWAP_PRICE_SPEC = RiskParameterSpec(
    output=RiskOutput.TWAP_PRICE,
    scoring_weight=Decimal("1.0"),
    grace_band=200,
    cutoff=2000,
    aggressive_direction=AggressiveDirection.HIGHER,
    lenient_multiplier=Decimal("3"),
    deviation_mode=DeviationMode.RELATIVE,
    unit="rao_per_alpha",
    confidence_floor_bps=DEFAULT_CONFIDENCE_FLOOR_BPS,
    scored_live=True,
    min_value=1,
    max_value=RISK_VALUE_MAX_INT64,
)
LIQUIDITY_DEPTH_SPEC = RiskParameterSpec(
    output=RiskOutput.LIQUIDITY_DEPTH,
    scoring_weight=Decimal("1.0"),
    grace_band=500,
    cutoff=5000,
    aggressive_direction=AggressiveDirection.HIGHER,
    lenient_multiplier=Decimal("3"),
    deviation_mode=DeviationMode.RELATIVE,
    unit="rao",
    confidence_floor_bps=DEFAULT_CONFIDENCE_FLOOR_BPS,
    scored_live=True,
    min_value=0,
    max_value=RISK_VALUE_MAX_INT64,
)

RISK_PARAMETER_SPECS = (
    MAX_DRAWDOWN_SPEC,
    REALIZED_VOLATILITY_SPEC,
    TWAP_PRICE_SPEC,
    LIQUIDITY_DEPTH_SPEC,
)
RISK_SPECS_BY_OUTPUT = {spec.output: spec for spec in RISK_PARAMETER_SPECS}


def _assert_specs_cover_all_outputs(specs: tuple[RiskParameterSpec, ...]) -> None:
    if tuple(spec.output for spec in specs) != tuple(RiskOutput):
        raise ValueError("risk specs must cover all V1 outputs in enum order")


_assert_specs_cover_all_outputs(RISK_PARAMETER_SPECS)


def build_risk_v1_subnet_alpha_schema() -> RiskSchema:
    """Build the V1 schema: four scored outputs at two horizons."""
    return RiskSchema(
        schema_id=RISK_SCHEMA_ID,
        target_type=SubnetAlphaTarget,
        context=RiskContextV1(),
        parameters=RISK_PARAMETER_SPECS,
    )


class RiskOutputValue(BaseModel):
    """One miner-submitted Alpha Risk output value and signed envelope fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    output: RiskOutput
    value: StrictInt
    confidence_bps: StrictInt
    reason_codes: tuple[str, ...]
    horizon_seconds: StrictInt
    unit: str

    @field_validator("confidence_bps")
    @classmethod
    def _validate_confidence(cls, value: int) -> int:
        return validate_confidence_bps(
            value, upper=MAX_CONFIDENCE_BPS, error_code=OUT_OF_BOUNDS_ERROR
        )

    @field_validator("horizon_seconds")
    @classmethod
    def _validate_horizon(cls, value: int) -> int:
        if value not in RISK_HORIZONS:
            raise ValueError(f"horizon_seconds must be one of {RISK_HORIZONS}: {value}")
        return value

    @field_validator("reason_codes")
    @classmethod
    def _validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return validate_reason_codes(value, policy=REASON_CODE_POLICY)

    @model_validator(mode="after")
    def _validate_against_spec(self) -> RiskOutputValue:
        validate_output_value_against_spec(
            submitted=OutputWireValue(
                output=self.output,
                value=self.value,
                confidence_bps=self.confidence_bps,
                unit=self.unit,
            ),
            spec=RISK_SPECS_BY_OUTPUT[self.output],
            error_code=OUT_OF_BOUNDS_ERROR,
        )
        return self


class RiskAssetSubmission(BaseModel):
    """All four Alpha Risk outputs at both horizons for one subnet Alpha."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    netuid: StrictInt
    outputs: tuple[RiskOutputValue, ...]

    @field_validator("netuid")
    @classmethod
    def _validate_netuid(cls, value: int) -> int:
        SubnetAlphaTarget(netuid=value)
        return value

    @field_validator("outputs")
    @classmethod
    def _validate_outputs(
        cls, value: tuple[RiskOutputValue, ...]
    ) -> tuple[RiskOutputValue, ...]:
        expected = {
            (output, horizon) for output in RiskOutput for horizon in RISK_HORIZONS
        }
        seen = {(item.output, item.horizon_seconds) for item in value}
        if len(seen) != len(value):
            raise ValueError("duplicate output/horizon for an asset")
        if seen != expected:
            missing = sorted(
                f"{output.value}:{horizon}" for output, horizon in expected - seen
            )
            raise ValueError(
                f"outputs must cover all V1 output/horizon combos; missing {missing}"
            )
        return value


class RiskSubmissionBundle(BaseModel):
    """One miner's full Alpha Risk round submission."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    round_id: str
    schema_id: Literal["risk.v1.subnet_alpha"]
    assets: tuple[RiskAssetSubmission, ...]

    @field_validator("round_id")
    @classmethod
    def _validate_round_id(cls, value: str) -> str:
        parsed = date.fromisoformat(value)
        if value != parsed.isoformat():
            raise ValueError("round_id must use YYYY-MM-DD")
        return value

    @field_validator("assets")
    @classmethod
    def _validate_assets(
        cls, value: tuple[RiskAssetSubmission, ...]
    ) -> tuple[RiskAssetSubmission, ...]:
        if not value:
            raise ValueError("assets must not be empty")
        if len(value) > MAX_RISK_ASSETS_PER_BUNDLE:
            raise ValueError(
                f"too many assets: {len(value)} > {MAX_RISK_ASSETS_PER_BUNDLE}"
            )
        netuids = [asset.netuid for asset in value]
        validate_unique_netuids(netuids)
        return value

    def to_canonical_payload(self) -> dict[str, object]:
        """Deterministic payload: assets by netuid, outputs by horizon and wire name."""
        return {
            "round_id": self.round_id,
            "schema_id": self.schema_id,
            "assets": canonical_assets_payload(
                self.assets,
                output_key=lambda item: (item.horizon_seconds, item.output.value),
            ),
        }
