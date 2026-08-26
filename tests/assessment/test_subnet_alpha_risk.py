from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import get_args

import pytest
from pydantic import ValidationError

from endure.assessment.registry import default_registry
from endure.assessment.schemas.forge_lending import AggressiveDirection, DeviationMode
from endure.assessment.schemas.subnet_alpha_risk import (
    HORIZON_5D_SECONDS,
    HORIZON_30D_SECONDS,
    MAX_REASON_CODES_PER_OUTPUT,
    RISK_HORIZONS,
    RISK_SCHEMA_ID,
    RISK_SPECS_BY_OUTPUT,
    RISK_VALUE_MAX_INT64,
    RiskAssetSubmission,
    RiskContextV1,
    RiskOutput,
    RiskOutputValue,
    RiskSubmissionBundle,
    SubnetAlphaTarget,
    build_risk_v1_subnet_alpha_schema,
)
from endure.assessment.subnet_alpha_universe import (
    ALPHA_RISK_WHITELISTED_NETUIDS,
    AlphaRiskUniverseError,
    StaticAlphaRiskUniverseProvider,
    canonical_alpha_risk_universe_members,
    parse_alpha_risk_universe_members,
    validate_alpha_risk_netuid_membership,
)
from endure.protocol.canonical import canonical_bundle_bytes
from endure.utils.config import active_runtime_schema_id, active_schema_id, config
from tests.utils.test_config import _FakeCls


def _risk_output(
    output: RiskOutput,
    value: int,
    *,
    horizon_seconds: int = HORIZON_5D_SECONDS,
    confidence_bps: int = 8000,
    reason_codes: tuple[str, ...] = ("baseline",),
) -> RiskOutputValue:
    spec = RISK_SPECS_BY_OUTPUT[output]
    return RiskOutputValue(
        output=output,
        value=value,
        confidence_bps=confidence_bps,
        reason_codes=reason_codes,
        horizon_seconds=horizon_seconds,
        unit=spec.unit,
    )


def _all_risk_outputs(
    *, reason_codes: tuple[str, ...] = ("baseline",)
) -> tuple[RiskOutputValue, ...]:
    values = {
        RiskOutput.MAX_DRAWDOWN: 1500,
        RiskOutput.REALIZED_VOLATILITY: 8000,
        RiskOutput.TWAP_PRICE: 123_000_000,
        RiskOutput.LIQUIDITY_DEPTH: 456_000_000,
    }
    return tuple(
        _risk_output(
            output,
            values[output],
            horizon_seconds=horizon,
            reason_codes=reason_codes,
        )
        for horizon in RISK_HORIZONS
        for output in RiskOutput
    )


class TestRiskSchema:
    def test_build_returns_four_outputs_scored_at_two_horizons(self) -> None:
        schema = build_risk_v1_subnet_alpha_schema()

        assert schema.schema_id == RISK_SCHEMA_ID
        assert isinstance(schema.context, RiskContextV1)
        assert schema.context.context_id == "risk.v1"
        assert schema.target_type is SubnetAlphaTarget
        assert tuple(spec.output for spec in schema.parameters) == tuple(RiskOutput)
        assert RISK_HORIZONS == (HORIZON_5D_SECONDS, HORIZON_30D_SECONDS)
        assert all(spec.scored_live for spec in schema.parameters)

    def test_scoring_table_matches_alpha_risk_scope(self) -> None:
        expected = {
            RiskOutput.MAX_DRAWDOWN: (
                AggressiveDirection.LOWER,
                DeviationMode.ABSOLUTE,
                200,
                2000,
                "bps",
                0,
                10000,
            ),
            RiskOutput.REALIZED_VOLATILITY: (
                AggressiveDirection.LOWER,
                DeviationMode.ABSOLUTE,
                500,
                5000,
                "bps",
                0,
                1_000_000,
            ),
            RiskOutput.TWAP_PRICE: (
                AggressiveDirection.HIGHER,
                DeviationMode.RELATIVE,
                200,
                2000,
                "rao_per_alpha",
                1,
                RISK_VALUE_MAX_INT64,
            ),
            RiskOutput.LIQUIDITY_DEPTH: (
                AggressiveDirection.HIGHER,
                DeviationMode.RELATIVE,
                500,
                5000,
                "rao",
                0,
                RISK_VALUE_MAX_INT64,
            ),
        }

        for output, values in expected.items():
            direction, mode, grace, cutoff, unit, minimum, maximum = values
            spec = RISK_SPECS_BY_OUTPUT[output]
            assert spec.aggressive_direction is direction
            assert spec.deviation_mode is mode
            assert spec.grace_band == grace
            assert spec.cutoff == cutoff
            assert spec.lenient_multiplier == Decimal(3)
            assert spec.unit == unit
            assert spec.min_value == minimum
            assert spec.max_value == maximum

    def test_output_bounds_are_enforced(self) -> None:
        with pytest.raises(ValidationError):
            _risk_output(RiskOutput.MAX_DRAWDOWN, 10001)
        with pytest.raises(ValidationError):
            _risk_output(RiskOutput.REALIZED_VOLATILITY, 1_000_001)
        with pytest.raises(ValidationError):
            _risk_output(RiskOutput.TWAP_PRICE, 0)
        with pytest.raises(ValidationError):
            _risk_output(RiskOutput.LIQUIDITY_DEPTH, RISK_VALUE_MAX_INT64 + 1)

    def test_submission_bundle_schema_literal_matches_schema_constant(self) -> None:
        annotation = RiskSubmissionBundle.model_fields["schema_id"].annotation
        assert get_args(annotation) == (RISK_SCHEMA_ID,)

    def test_parameter_spec_rejects_invalid_invariants(self) -> None:
        with pytest.raises(ValueError):
            replace(RISK_SPECS_BY_OUTPUT[RiskOutput.MAX_DRAWDOWN], cutoff=200)


class TestRiskBundle:
    def test_asset_requires_all_four_outputs_at_both_horizons(self) -> None:
        asset = RiskAssetSubmission(netuid=44, outputs=_all_risk_outputs())

        assert len(asset.outputs) == 8

    def test_missing_output_horizon_combo_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="missing"):
            RiskAssetSubmission(netuid=44, outputs=_all_risk_outputs()[:-1])

    def test_duplicate_output_horizon_combo_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate"):
            RiskAssetSubmission(
                netuid=44,
                outputs=(
                    *_all_risk_outputs(),
                    _risk_output(RiskOutput.MAX_DRAWDOWN, 1),
                ),
            )

    def test_wrong_horizon_and_unit_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _risk_output(RiskOutput.MAX_DRAWDOWN, 1500, horizon_seconds=1)
        with pytest.raises(ValidationError):
            RiskOutputValue(
                output=RiskOutput.MAX_DRAWDOWN,
                value=1500,
                confidence_bps=8000,
                reason_codes=(),
                horizon_seconds=HORIZON_5D_SECONDS,
                unit="rao",
            )

    def test_canonical_payload_is_order_independent(self) -> None:
        reason_codes = ("volatility_high", "baseline")
        asset_44 = RiskAssetSubmission(
            netuid=44,
            outputs=_all_risk_outputs(reason_codes=reason_codes),
        )
        asset_8 = RiskAssetSubmission(
            netuid=8,
            outputs=tuple(
                reversed(_all_risk_outputs(reason_codes=tuple(reversed(reason_codes))))
            ),
        )
        bundle = RiskSubmissionBundle(
            round_id="2026-07-06",
            schema_id=RISK_SCHEMA_ID,
            assets=(asset_44, asset_8),
        )
        reordered = RiskSubmissionBundle(
            round_id="2026-07-06",
            schema_id=RISK_SCHEMA_ID,
            assets=(asset_8, asset_44),
        )

        assert canonical_bundle_bytes(
            bundle.to_canonical_payload()
        ) == canonical_bundle_bytes(reordered.to_canonical_payload())

    @pytest.mark.parametrize(
        "reason_codes",
        [
            ("baseline", "baseline"),
            ("Baseline",),
            ("x" * 65,),
            tuple(f"r{i}" for i in range(MAX_REASON_CODES_PER_OUTPUT + 1)),
        ],
    )
    def test_malformed_reason_codes_are_rejected(
        self, reason_codes: tuple[str, ...]
    ) -> None:
        with pytest.raises(ValidationError):
            _risk_output(RiskOutput.MAX_DRAWDOWN, 1500, reason_codes=reason_codes)


class TestAlphaRiskUniverse:
    def test_static_provider_returns_versioned_launch_whitelist_snapshot(self) -> None:
        provider = StaticAlphaRiskUniverseProvider()

        snapshot = provider.fetch_universe("2026-07-06")

        assert snapshot.tickers == tuple(
            str(netuid) for netuid in sorted(ALPHA_RISK_WHITELISTED_NETUIDS)
        )
        assert {8, 44}.issubset(ALPHA_RISK_WHITELISTED_NETUIDS)
        assert len(ALPHA_RISK_WHITELISTED_NETUIDS) == 12
        assert len(snapshot.source_hash) == 64

    def test_provider_rejects_duplicates_and_cap_excess(self) -> None:
        with pytest.raises(AlphaRiskUniverseError, match="unique"):
            StaticAlphaRiskUniverseProvider(netuids=(8, 8)).fetch_universe("2026-07-06")
        with pytest.raises(AlphaRiskUniverseError, match="targets > cap"):
            StaticAlphaRiskUniverseProvider(
                netuids=(8, 44), max_targets=1
            ).fetch_universe("2026-07-06")

    def test_membership_parse_fails_closed_on_noncanonical_members(self) -> None:
        universe = canonical_alpha_risk_universe_members((44, 8))

        assert validate_alpha_risk_netuid_membership(netuids=(8, 44), universe=universe)
        assert not validate_alpha_risk_netuid_membership(
            netuids=(1,), universe=universe
        )
        with pytest.raises(AlphaRiskUniverseError, match="invalid alpha risk netuid"):
            parse_alpha_risk_universe_members(("044",))


class TestRegistryAndConfig:
    def test_default_registry_registers_risk_served_with_universe(self) -> None:
        entry = default_registry().get(RISK_SCHEMA_ID)

        assert entry.schema.schema_id == RISK_SCHEMA_ID
        assert entry.bundle_model is RiskSubmissionBundle
        assert entry.serving_status == "served"
        assert isinstance(entry.universe_provider, StaticAlphaRiskUniverseProvider)

    def test_config_defaults_to_served_risk_schema(self) -> None:
        cfg = config(_FakeCls)

        assert active_schema_id(cfg) == RISK_SCHEMA_ID
        assert active_runtime_schema_id(cfg) == RISK_SCHEMA_ID
