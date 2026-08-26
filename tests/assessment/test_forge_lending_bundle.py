from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from endure.assessment.registry import default_registry
from endure.assessment.schemas.forge_lending import (
    FORGE_LENDING_SCHEMA_ID,
    LENDING_HORIZON_SECONDS,
    LENDING_PARAMETER_SPECS,
    MAX_LENDING_ASSETS_PER_BUNDLE,
    MAX_REASON_CODES_PER_OUTPUT,
    ONE_E18,
    LendingAssetSubmission,
    LendingOutput,
    LendingOutputValue,
    LendingSubmissionBundle,
)
from endure.protocol.canonical import canonical_bundle_bytes


def _output(
    output: LendingOutput,
    value: int,
    unit: str,
    *,
    confidence_bps: int = 8000,
    horizon_seconds: int = LENDING_HORIZON_SECONDS,
    reason_codes: tuple[str, ...] = ("thin_liquidity",),
) -> LendingOutputValue:
    return LendingOutputValue(
        output=output,
        value=value,
        confidence_bps=confidence_bps,
        reason_codes=reason_codes,
        horizon_seconds=horizon_seconds,
        unit=unit,
    )


def _all_outputs(
    *,
    collateral_factor: int = 2500,
    liquidation_threshold: int = 3500,
    reason_codes: tuple[str, ...] = ("thin_liquidity",),
) -> tuple[LendingOutputValue, ...]:
    return (
        _output(
            LendingOutput.SAFE_ASSET_PRICE,
            ONE_E18,
            "price_1e18",
            reason_codes=reason_codes,
        ),
        _output(
            LendingOutput.COLLATERAL_FACTOR,
            collateral_factor,
            "ltv_bps",
            reason_codes=reason_codes,
        ),
        _output(
            LendingOutput.LIQUIDATION_THRESHOLD,
            liquidation_threshold,
            "ltv_bps",
            reason_codes=reason_codes,
        ),
        _output(
            LendingOutput.LIQUIDATION_INCENTIVE,
            int(Decimal("1.08") * ONE_E18),
            "mantissa_1e18",
            reason_codes=reason_codes,
        ),
        _output(
            LendingOutput.SUPPLY_CAP,
            0,
            "underlying_units",
            reason_codes=reason_codes,
        ),
        _output(LendingOutput.BORROW_CAP, 0, "tao_units", reason_codes=reason_codes),
        _output(LendingOutput.RISK_TIER, 3, "ordinal", reason_codes=reason_codes),
    )


class TestLendingBundle:
    def test_round_trips_canonical_payload(self) -> None:
        bundle = LendingSubmissionBundle(
            round_id="2026-06-09",
            schema_id=FORGE_LENDING_SCHEMA_ID,
            assets=(LendingAssetSubmission(netuid=30, outputs=_all_outputs()),),
        )

        payload = bundle.to_canonical_payload()

        assert payload["assets"][0]["netuid"] == 30
        outputs = {item["output"]: item for item in payload["assets"][0]["outputs"]}
        assert outputs["collateral_factor"]["value"] == 2500
        canonical_bundle_bytes(payload)

    def test_canonical_payload_is_order_independent(self) -> None:
        reason_codes = ("thin_liquidity", "volatility_high")
        asset_7 = LendingAssetSubmission(
            netuid=7,
            outputs=_all_outputs(reason_codes=reason_codes),
        )
        asset_30 = LendingAssetSubmission(
            netuid=30,
            outputs=_all_outputs(reason_codes=reason_codes),
        )
        bundle = LendingSubmissionBundle(
            round_id="2026-06-09",
            schema_id=FORGE_LENDING_SCHEMA_ID,
            assets=(asset_30, asset_7),
        )
        reordered = LendingSubmissionBundle(
            round_id="2026-06-09",
            schema_id=FORGE_LENDING_SCHEMA_ID,
            assets=(
                LendingAssetSubmission(
                    netuid=7,
                    outputs=tuple(
                        reversed(
                            _all_outputs(reason_codes=tuple(reversed(reason_codes)))
                        )
                    ),
                ),
                LendingAssetSubmission(
                    netuid=30,
                    outputs=tuple(
                        reversed(
                            _all_outputs(reason_codes=tuple(reversed(reason_codes)))
                        )
                    ),
                ),
            ),
        )

        assert canonical_bundle_bytes(
            bundle.to_canonical_payload()
        ) == canonical_bundle_bytes(reordered.to_canonical_payload())

    def test_confidence_out_of_range_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LendingOutputValue(
                output=LendingOutput.COLLATERAL_FACTOR,
                value=2500,
                confidence_bps=10001,
                reason_codes=(),
                horizon_seconds=LENDING_HORIZON_SECONDS,
                unit="ltv_bps",
            )

    def test_confidence_below_output_floor_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _output(
                LendingOutput.COLLATERAL_FACTOR,
                2500,
                "ltv_bps",
                confidence_bps=500,
            )

    def test_duplicate_output_for_an_asset_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LendingAssetSubmission(
                netuid=30,
                outputs=(
                    *_all_outputs(),
                    _output(LendingOutput.COLLATERAL_FACTOR, 9000, "ltv_bps"),
                ),
            )

    def test_missing_output_for_an_asset_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LendingAssetSubmission(netuid=30, outputs=_all_outputs()[:-1])

    def test_empty_outputs_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LendingAssetSubmission(netuid=30, outputs=())

    def test_empty_assets_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LendingSubmissionBundle(
                round_id="2026-06-09",
                schema_id=FORGE_LENDING_SCHEMA_ID,
                assets=(),
            )

    def test_too_many_assets_are_rejected(self) -> None:
        outputs = _all_outputs()
        assets = tuple(
            LendingAssetSubmission(netuid=netuid, outputs=outputs)
            for netuid in range(MAX_LENDING_ASSETS_PER_BUNDLE + 1)
        )

        with pytest.raises(ValidationError):
            LendingSubmissionBundle(
                round_id="2026-06-09",
                schema_id=FORGE_LENDING_SCHEMA_ID,
                assets=assets,
            )

    def test_duplicate_netuid_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LendingSubmissionBundle(
                round_id="2026-06-09",
                schema_id=FORGE_LENDING_SCHEMA_ID,
                assets=(
                    LendingAssetSubmission(netuid=30, outputs=_all_outputs()),
                    LendingAssetSubmission(netuid=30, outputs=_all_outputs()),
                ),
            )

    def test_round_id_must_be_canonical_yyyy_mm_dd(self) -> None:
        with pytest.raises(ValidationError):
            LendingSubmissionBundle(
                round_id="20260609",
                schema_id=FORGE_LENDING_SCHEMA_ID,
                assets=(LendingAssetSubmission(netuid=30, outputs=_all_outputs()),),
            )

    def test_wrong_unit_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _output(LendingOutput.COLLATERAL_FACTOR, 2500, "mantissa_1e18")

    def test_non_uniform_horizon_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _output(
                LendingOutput.COLLATERAL_FACTOR,
                2500,
                "ltv_bps",
                horizon_seconds=1,
            )

    @pytest.mark.parametrize(
        "reason_codes",
        [
            ("thin_liquidity", "thin_liquidity"),
            ("ThinLiquidity",),
            ("x" * 65,),
            tuple(f"r{i}" for i in range(MAX_REASON_CODES_PER_OUTPUT + 1)),
        ],
    )
    def test_malformed_reason_codes_are_rejected(
        self, reason_codes: tuple[str, ...]
    ) -> None:
        with pytest.raises(ValidationError):
            _output(
                LendingOutput.COLLATERAL_FACTOR,
                2500,
                "ltv_bps",
                reason_codes=reason_codes,
            )

    def test_risk_tier_out_of_range_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _output(LendingOutput.RISK_TIER, 7, "ordinal")

    def test_cf_above_liquidation_threshold_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LendingAssetSubmission(
                netuid=30,
                outputs=_all_outputs(
                    collateral_factor=4000,
                    liquidation_threshold=3500,
                ),
            )


class TestOutputBounds:
    def test_safe_asset_price_above_max_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _output(LendingOutput.SAFE_ASSET_PRICE, 10**31, "price_1e18")

    def test_supply_cap_above_max_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _output(LendingOutput.SUPPLY_CAP, 10**31, "underlying_units")

    def test_borrow_cap_above_max_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _output(LendingOutput.BORROW_CAP, 10**31, "tao_units")

    def test_liquidation_incentive_above_max_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _output(LendingOutput.LIQUIDATION_INCENTIVE, 3 * ONE_E18, "mantissa_1e18")

    def test_every_output_spec_has_a_finite_ceiling(self) -> None:
        from endure.assessment.schemas.forge_lending import LENDING_PARAMETER_SPECS

        assert all(spec.max_value is not None for spec in LENDING_PARAMETER_SPECS)


class TestStrictIntegerFields:
    @pytest.mark.parametrize("bad", [True, 2500.0, "2500"])
    def test_value_rejects_non_int(self, bad: object) -> None:
        with pytest.raises(ValidationError):
            _output(LendingOutput.COLLATERAL_FACTOR, bad, "ltv_bps")  # type: ignore[arg-type]

    def test_netuid_rejects_bool(self) -> None:
        with pytest.raises(ValidationError):
            LendingAssetSubmission(netuid=True, outputs=_all_outputs())  # type: ignore[arg-type]


class TestServingGateOnPlaceholderCeilings:
    # The 10**30 ceilings satisfy S-15's bounded-wire-integer requirement but
    # are explicit placeholders: the schema comment requires tightening them
    # to Forge's confirmed parameter ranges before lending is served. Comments
    # don't gate anything — this test does: it fails the moment the registry
    # flips lending to "served" with a placeholder ceiling still in place.
    PLACEHOLDER_CEILING = 10**30

    def test_serving_flip_is_blocked_while_placeholder_ceilings_remain(self) -> None:
        entry = default_registry().get(FORGE_LENDING_SCHEMA_ID)
        placeholder_outputs = [
            spec.output.value
            for spec in LENDING_PARAMETER_SPECS
            if spec.max_value is not None and spec.max_value >= self.PLACEHOLDER_CEILING
        ]

        assert entry.serving_status != "served" or not placeholder_outputs, (
            "lending is served with placeholder ceilings still in place for "
            f"{placeholder_outputs}; tighten them to Forge's confirmed "
            "parameter ranges first (Forge lending scope spec §Batch 8)"
        )

    def test_gate_currently_tracks_three_placeholder_outputs(self) -> None:
        # Locks the gate to reality: if the ceilings are tightened, this list
        # shrinks and the assertion here should be updated to empty.
        placeholder_outputs = {
            spec.output
            for spec in LENDING_PARAMETER_SPECS
            if spec.max_value is not None and spec.max_value >= self.PLACEHOLDER_CEILING
        }
        assert placeholder_outputs == {
            LendingOutput.SAFE_ASSET_PRICE,
            LendingOutput.SUPPLY_CAP,
            LendingOutput.BORROW_CAP,
        }
