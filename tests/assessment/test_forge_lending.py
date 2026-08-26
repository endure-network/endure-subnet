from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import get_args

import pytest

from endure.assessment.schemas.forge_lending import (
    COLLATERAL_FACTOR_SPEC,
    FORGE_LENDING_SCHEMA_ID,
    AggressiveDirection,
    DeviationMode,
    LendingContextV1,
    LendingOutput,
    LendingParameterSpec,
    LendingSchema,
    LendingSubmissionBundle,
    SubnetAssetTarget,
    build_lending_v1_subnet_asset_schema,
)


class TestLendingTypes:
    def test_subnet_asset_target_carries_netuid(self) -> None:
        assert SubnetAssetTarget(netuid=30).netuid == 30

    def test_negative_netuid_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            SubnetAssetTarget(netuid=-1)

    def test_context_has_stable_id(self) -> None:
        assert LendingContextV1().context_id == "lending.v1"

    def test_output_enum_values_are_snake_case_wire_names(self) -> None:
        assert LendingOutput.COLLATERAL_FACTOR.value == "collateral_factor"
        assert LendingOutput.BORROW_CAP.value == "borrow_cap"

    def test_aggressive_direction_values(self) -> None:
        assert AggressiveDirection.HIGHER.value == "higher"

    def test_deviation_mode_values(self) -> None:
        assert DeviationMode.ABSOLUTE.value == "absolute"
        assert DeviationMode.RELATIVE.value == "relative"


class TestLendingSchema:
    def test_build_returns_all_seven_v1_outputs(self) -> None:
        schema = build_lending_v1_subnet_asset_schema()
        assert schema.schema_id == FORGE_LENDING_SCHEMA_ID
        assert isinstance(schema, LendingSchema)
        outputs = tuple(spec.output for spec in schema.parameters)
        assert outputs == tuple(LendingOutput)

    def test_only_collateral_factor_is_scored_live_in_plan_1(self) -> None:
        schema = build_lending_v1_subnet_asset_schema()
        live_outputs = tuple(
            spec.output for spec in schema.parameters if spec.scored_live
        )
        assert live_outputs == (LendingOutput.COLLATERAL_FACTOR,)

    def test_collateral_factor_spec_is_asymmetric_higher(self) -> None:
        by_output = {
            spec.output: spec
            for spec in build_lending_v1_subnet_asset_schema().parameters
        }
        spec = by_output[LendingOutput.COLLATERAL_FACTOR]
        assert isinstance(spec, LendingParameterSpec)
        assert spec.aggressive_direction.value == "higher"  # too-high CF is dangerous
        assert spec.grace_band > 0 and spec.cutoff > spec.grace_band
        assert spec.lenient_multiplier > Decimal(1)
        assert spec.unit == "ltv_bps"

    def test_deviation_mode_is_absolute_for_ratios_relative_for_scaled(
        self,
    ) -> None:
        by_output = {
            spec.output: spec
            for spec in build_lending_v1_subnet_asset_schema().parameters
        }
        assert (
            by_output[LendingOutput.COLLATERAL_FACTOR].deviation_mode
            is DeviationMode.ABSOLUTE
        )
        assert (
            by_output[LendingOutput.RISK_TIER].deviation_mode is DeviationMode.ABSOLUTE
        )
        assert (
            by_output[LendingOutput.SAFE_ASSET_PRICE].deviation_mode
            is DeviationMode.RELATIVE
        )
        assert (
            by_output[LendingOutput.SUPPLY_CAP].deviation_mode is DeviationMode.RELATIVE
        )
        for spec in by_output.values():
            assert spec.cutoff > spec.grace_band

    def test_units_and_bounds_are_schema_embedded(self) -> None:
        by_output = {
            spec.output: spec
            for spec in build_lending_v1_subnet_asset_schema().parameters
        }
        assert by_output[LendingOutput.LIQUIDATION_THRESHOLD].unit == "ltv_bps"
        assert by_output[LendingOutput.RISK_TIER].min_value == 1
        assert by_output[LendingOutput.RISK_TIER].max_value == 6

    def test_submission_bundle_schema_literal_matches_schema_constant(self) -> None:
        annotation = LendingSubmissionBundle.model_fields["schema_id"].annotation
        assert get_args(annotation) == (FORGE_LENDING_SCHEMA_ID,)

    def test_parameter_spec_rejects_invalid_invariants(self) -> None:
        with pytest.raises(ValueError):
            replace(COLLATERAL_FACTOR_SPEC, scoring_weight=Decimal("0"))
        with pytest.raises(ValueError):
            replace(COLLATERAL_FACTOR_SPEC, grace_band=-1)
        with pytest.raises(ValueError):
            replace(
                COLLATERAL_FACTOR_SPEC,
                cutoff=COLLATERAL_FACTOR_SPEC.grace_band,
            )
        with pytest.raises(ValueError):
            replace(COLLATERAL_FACTOR_SPEC, lenient_multiplier=Decimal("0.5"))
        with pytest.raises(ValueError):
            replace(COLLATERAL_FACTOR_SPEC, confidence_floor_bps=10001)
        with pytest.raises(ValueError):
            replace(COLLATERAL_FACTOR_SPEC, min_value=-1)
        with pytest.raises(ValueError):
            replace(COLLATERAL_FACTOR_SPEC, max_value=0)
