"""Forge lending parameter schema (Forge lending scope spec §V1 scored schema).

Miners recommend per-Alpha-market risk parameters that Forge (a Venus Core Pool
fork) consumes. V1 is sim-free: outputs ground against realized observables.
Lending defines its own three-axis types here rather than reusing the
forecast-shaped AssessmentSchema/ParameterSpec. Numeric constants are
provisional starting values subject to testnet validation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictInt,
    field_validator,
    model_validator,
)

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

FORGE_LENDING_SCHEMA_ID = "lending.v1.subnet_asset"
# Uniform-round single horizon for V1 (per-output horizons are a V1.1 feature).
LENDING_HORIZON_TRADING_DAYS = 5
LENDING_HORIZON_SECONDS = LENDING_HORIZON_TRADING_DAYS * 24 * 60 * 60
MAX_LENDING_ASSETS_PER_BUNDLE = 256
MAX_REASON_CODES_PER_OUTPUT = 8
MAX_REASON_CODE_LENGTH = 64
REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
REASON_CODE_POLICY = ReasonCodePolicy(
    max_codes=MAX_REASON_CODES_PER_OUTPUT,
    max_length=MAX_REASON_CODE_LENGTH,
    pattern=REASON_CODE_PATTERN,
)


@dataclass(frozen=True, slots=True)
class SubnetAssetTarget:
    """Axis 1 - a Bittensor subnet alpha-token, identified by netuid."""

    netuid: int

    def __post_init__(self) -> None:
        if self.netuid < 0:
            raise ValueError(f"netuid must be non-negative: {self.netuid}")


@dataclass(frozen=True, slots=True)
class LendingContextV1:
    """Axis 2 - Forge lending-parameter recommendation."""

    context_id: str = "lending.v1"


class LendingOutput(StrEnum):
    """The seven V1 output wire names."""

    SAFE_ASSET_PRICE = "safe_asset_price"
    COLLATERAL_FACTOR = "collateral_factor"
    LIQUIDATION_THRESHOLD = "liquidation_threshold"
    LIQUIDATION_INCENTIVE = "liquidation_incentive"
    SUPPLY_CAP = "supply_cap"
    BORROW_CAP = "borrow_cap"
    RISK_TIER = "risk_tier"


@dataclass(frozen=True, slots=True)
class LendingParameterSpec(BaseParameterSpec[LendingOutput]):
    """One scored lending output and its asymmetric deviation bands.

    ``cutoff`` is the total deviation where score reaches zero; it includes the
    grace band. ``scored_live`` marks which outputs currently have live
    realized-observable scoring while the full seven-output envelope is already
    collected.
    """


@dataclass(frozen=True, slots=True)
class LendingSchema:
    """Axis 3 - the lending output bundle."""

    schema_id: str
    target_type: type
    context: LendingContextV1
    parameters: tuple[LendingParameterSpec, ...]


MAX_LTV_BPS = 10000
MAX_CONFIDENCE_BPS = 10000
ONE_E18 = 10**18
DEFAULT_CONFIDENCE_FLOOR_BPS = 1000
COLLATERAL_FACTOR_LIQUIDATION_BUFFER_BPS = 500

# Product-aware ceilings for the outputs that are otherwise unbounded integers
# (S-15). These bound the signed wire value so a miner cannot submit an
# arbitrary-length integer; they are deliberately generous relative to any
# plausible Alpha market and should be tightened to Forge's confirmed
# parameter ranges before lending is served.
#   - price: <= 1e12 whole units at 1e18 fixed point.
#   - liquidation incentive: a 2.0x multiplier (100% bonus) hard ceiling; real
#     values sit near 1.05-1.20x.
#   - supply/borrow caps: well above any Alpha/TAO supply even in base units.
MAX_SAFE_ASSET_PRICE_1E18 = 10**30
MAX_LIQUIDATION_INCENTIVE_1E18 = 2 * ONE_E18
MAX_SUPPLY_CAP_UNITS = 10**30
MAX_BORROW_CAP_TAO_UNITS = 10**30

# Lending weights are relative signal-strength weights, not a partition of
# unity. The fan-out aggregator must normalize by sum(scoring_weight).
SAFE_ASSET_PRICE_SPEC = LendingParameterSpec(
    output=LendingOutput.SAFE_ASSET_PRICE,
    scoring_weight=Decimal("1.0"),
    grace_band=50,
    cutoff=1000,
    aggressive_direction=AggressiveDirection.HIGHER,
    lenient_multiplier=Decimal("3"),
    deviation_mode=DeviationMode.RELATIVE,
    unit="price_1e18",
    confidence_floor_bps=DEFAULT_CONFIDENCE_FLOOR_BPS,
    scored_live=False,
    min_value=1,
    max_value=MAX_SAFE_ASSET_PRICE_1E18,
)

COLLATERAL_FACTOR_SPEC = LendingParameterSpec(
    output=LendingOutput.COLLATERAL_FACTOR,
    scoring_weight=Decimal("1.0"),
    grace_band=200,
    cutoff=1500,
    aggressive_direction=AggressiveDirection.HIGHER,
    lenient_multiplier=Decimal("3"),
    deviation_mode=DeviationMode.ABSOLUTE,
    unit="ltv_bps",
    confidence_floor_bps=DEFAULT_CONFIDENCE_FLOOR_BPS,
    scored_live=True,
    min_value=1,
    max_value=MAX_LTV_BPS,
)

LIQUIDATION_THRESHOLD_SPEC = LendingParameterSpec(
    output=LendingOutput.LIQUIDATION_THRESHOLD,
    scoring_weight=Decimal("0.5"),
    grace_band=200,
    cutoff=1500,
    aggressive_direction=AggressiveDirection.HIGHER,
    lenient_multiplier=Decimal("3"),
    deviation_mode=DeviationMode.ABSOLUTE,
    unit="ltv_bps",
    confidence_floor_bps=DEFAULT_CONFIDENCE_FLOOR_BPS,
    scored_live=False,
    min_value=1,
    max_value=MAX_LTV_BPS,
)

# Fan-out note: scoring the raw 1e18 mantissa compresses incentive bonus
# differences because the base dominates; score value - 1e18 when LI goes live.
LIQUIDATION_INCENTIVE_SPEC = LendingParameterSpec(
    output=LendingOutput.LIQUIDATION_INCENTIVE,
    scoring_weight=Decimal("0.25"),
    grace_band=50,
    cutoff=500,
    aggressive_direction=AggressiveDirection.LOWER,
    lenient_multiplier=Decimal("2"),
    deviation_mode=DeviationMode.RELATIVE,
    unit="mantissa_1e18",
    confidence_floor_bps=DEFAULT_CONFIDENCE_FLOOR_BPS,
    scored_live=False,
    min_value=ONE_E18,
    max_value=MAX_LIQUIDATION_INCENTIVE_1E18,
)

SUPPLY_CAP_SPEC = LendingParameterSpec(
    output=LendingOutput.SUPPLY_CAP,
    scoring_weight=Decimal("1.0"),
    grace_band=500,
    cutoff=5000,
    aggressive_direction=AggressiveDirection.HIGHER,
    lenient_multiplier=Decimal("3"),
    deviation_mode=DeviationMode.RELATIVE,
    unit="underlying_units",
    confidence_floor_bps=DEFAULT_CONFIDENCE_FLOOR_BPS,
    scored_live=False,
    min_value=0,
    max_value=MAX_SUPPLY_CAP_UNITS,
)

BORROW_CAP_SPEC = LendingParameterSpec(
    output=LendingOutput.BORROW_CAP,
    scoring_weight=Decimal("0.5"),
    grace_band=500,
    cutoff=5000,
    aggressive_direction=AggressiveDirection.HIGHER,
    lenient_multiplier=Decimal("3"),
    deviation_mode=DeviationMode.RELATIVE,
    unit="tao_units",
    confidence_floor_bps=DEFAULT_CONFIDENCE_FLOOR_BPS,
    scored_live=False,
    min_value=0,
    max_value=MAX_BORROW_CAP_TAO_UNITS,
)

# Tiers increase with risk, so under-rating risk is a too-low tier; LOWER is the
# aggressive side for future risk-tier scoring.
RISK_TIER_SPEC = LendingParameterSpec(
    output=LendingOutput.RISK_TIER,
    scoring_weight=Decimal("1.0"),
    grace_band=0,
    cutoff=3,
    aggressive_direction=AggressiveDirection.LOWER,
    lenient_multiplier=Decimal("2"),
    deviation_mode=DeviationMode.ABSOLUTE,
    unit="ordinal",
    confidence_floor_bps=DEFAULT_CONFIDENCE_FLOOR_BPS,
    scored_live=False,
    min_value=1,
    max_value=6,
)

LENDING_PARAMETER_SPECS = (
    SAFE_ASSET_PRICE_SPEC,
    COLLATERAL_FACTOR_SPEC,
    LIQUIDATION_THRESHOLD_SPEC,
    LIQUIDATION_INCENTIVE_SPEC,
    SUPPLY_CAP_SPEC,
    BORROW_CAP_SPEC,
    RISK_TIER_SPEC,
)

LENDING_SPECS_BY_OUTPUT = {spec.output: spec for spec in LENDING_PARAMETER_SPECS}


def _assert_specs_cover_all_outputs(specs: tuple[LendingParameterSpec, ...]) -> None:
    if tuple(spec.output for spec in specs) != tuple(LendingOutput):
        raise ValueError("lending specs must cover all V1 outputs in enum order")


_assert_specs_cover_all_outputs(LENDING_PARAMETER_SPECS)


def build_lending_v1_subnet_asset_schema() -> LendingSchema:
    """Build the V1 schema: all seven outputs, only CF scored live."""
    return LendingSchema(
        schema_id=FORGE_LENDING_SCHEMA_ID,
        target_type=SubnetAssetTarget,
        context=LendingContextV1(),
        parameters=LENDING_PARAMETER_SPECS,
    )


class LendingOutputValue(BaseModel):
    """One miner-submitted output value and signed envelope fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Signed wire values are strict integers (C-03): reject bool, float, and
    # string so no coercion happens at the signed payload boundary.
    output: LendingOutput
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
        if value != LENDING_HORIZON_SECONDS:
            raise ValueError(
                f"horizon_seconds must be {LENDING_HORIZON_SECONDS}: {value}"
            )
        return value

    @field_validator("reason_codes")
    @classmethod
    def _validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return validate_reason_codes(value, policy=REASON_CODE_POLICY)

    @model_validator(mode="after")
    def _validate_against_spec(self) -> LendingOutputValue:
        validate_output_value_against_spec(
            submitted=OutputWireValue(
                output=self.output,
                value=self.value,
                confidence_bps=self.confidence_bps,
                unit=self.unit,
            ),
            spec=LENDING_SPECS_BY_OUTPUT[self.output],
            error_code=OUT_OF_BOUNDS_ERROR,
        )
        return self


class LendingAssetSubmission(BaseModel):
    """All seven output values a miner recommends for one Alpha market."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    netuid: StrictInt
    outputs: tuple[LendingOutputValue, ...]

    @field_validator("netuid")
    @classmethod
    def _validate_netuid(cls, value: int) -> int:
        SubnetAssetTarget(netuid=value)
        return value

    @field_validator("outputs")
    @classmethod
    def _validate_outputs(
        cls, value: tuple[LendingOutputValue, ...]
    ) -> tuple[LendingOutputValue, ...]:
        if not value:
            raise ValueError("outputs must not be empty")
        seen = {item.output for item in value}
        if len(seen) != len(value):
            raise ValueError("duplicate output for an asset")
        expected = set(LendingOutput)
        if seen != expected:
            missing = sorted(output.value for output in expected - seen)
            raise ValueError(f"outputs must cover all V1 outputs; missing {missing}")
        return value

    @model_validator(mode="after")
    def _validate_cross_output_invariants(self) -> LendingAssetSubmission:
        values = {item.output: item.value for item in self.outputs}
        if (
            values[LendingOutput.COLLATERAL_FACTOR]
            > values[LendingOutput.LIQUIDATION_THRESHOLD]
        ):
            raise ValueError("collateral_factor must be <= liquidation_threshold")
        return self


class LendingSubmissionBundle(BaseModel):
    """One miner's full lending-round submission."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    round_id: str
    schema_id: Literal["lending.v1.subnet_asset"]
    assets: tuple[LendingAssetSubmission, ...]

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
        cls, value: tuple[LendingAssetSubmission, ...]
    ) -> tuple[LendingAssetSubmission, ...]:
        if not value:
            raise ValueError("assets must not be empty")
        if len(value) > MAX_LENDING_ASSETS_PER_BUNDLE:
            raise ValueError(
                f"too many assets: {len(value)} > {MAX_LENDING_ASSETS_PER_BUNDLE}"
            )
        netuids = [asset.netuid for asset in value]
        validate_unique_netuids(netuids)
        return value

    def to_canonical_payload(self) -> dict[str, object]:
        """Deterministic payload: assets by netuid and outputs by wire name."""
        return {
            "round_id": self.round_id,
            "schema_id": self.schema_id,
            "assets": canonical_assets_payload(
                self.assets,
                output_key=lambda item: (0, item.output.value),
            ),
        }
