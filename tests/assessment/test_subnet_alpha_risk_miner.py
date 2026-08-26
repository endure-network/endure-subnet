from __future__ import annotations

import json

from endure.assessment.schemas.subnet_alpha_risk import (
    HORIZON_5D_SECONDS,
    HORIZON_30D_SECONDS,
    RISK_SCHEMA_ID,
    RiskOutput,
)
from endure.protocol.risk_miner import (
    LatestPoolObservation,
    RiskBaselineAssembler,
    baseline_risk_bundle,
)


class TestRiskBaselineAssembler:
    def test_baseline_uses_constants_and_latest_observations(self) -> None:
        observations = {
            8: LatestPoolObservation(price_rao=12_000_000_000, tao_reserve_rao=99),
            44: LatestPoolObservation(price_rao=44_000_000_000, tao_reserve_rao=123),
        }

        bundle = baseline_risk_bundle(
            round_id="2026-07-06",
            netuids=(44, 8),
            latest_observation=observations.get,
        )

        by_netuid = {asset.netuid: asset for asset in bundle.assets}
        assert tuple(by_netuid) == (8, 44)
        outputs = {
            (item.output, item.horizon_seconds): item for item in by_netuid[8].outputs
        }
        assert outputs[(RiskOutput.MAX_DRAWDOWN, HORIZON_5D_SECONDS)].value == 1500
        assert outputs[(RiskOutput.MAX_DRAWDOWN, HORIZON_30D_SECONDS)].value == 3000
        assert (
            outputs[(RiskOutput.REALIZED_VOLATILITY, HORIZON_5D_SECONDS)].value == 8000
        )
        assert (
            outputs[(RiskOutput.TWAP_PRICE, HORIZON_5D_SECONDS)].value == 12_000_000_000
        )
        assert outputs[(RiskOutput.LIQUIDITY_DEPTH, HORIZON_30D_SECONDS)].value == 99

    def test_baseline_skips_netuid_with_missing_observation(self) -> None:
        bundle = baseline_risk_bundle(
            round_id="2026-07-06",
            netuids=(8, 44),
            latest_observation=lambda netuid: (
                LatestPoolObservation(price_rao=1, tao_reserve_rao=2)
                if netuid == 44
                else None
            ),
        )

        assert tuple(asset.netuid for asset in bundle.assets) == (44,)

    def test_assembler_serializes_canonical_bundle(self) -> None:
        assembler = RiskBaselineAssembler(
            netuids=(8,),
            miner_hotkey="hk-a",
            latest_observation=lambda netuid: LatestPoolObservation(
                price_rao=netuid + 1, tao_reserve_rao=netuid + 2
            ),
        )

        assembled = assembler("2026-07-06")

        payload = json.loads(assembled.bundle_json)
        assert payload["schema_id"] == RISK_SCHEMA_ID
        assert assembled.bundle_hash
