from __future__ import annotations

from decimal import Decimal

from custom_miner.adaptive import (
    ADAPTIVE_REASON_CODE,
    adaptive_risk_bundle,
)
from endure.assessment.schemas.subnet_alpha_risk import (
    HORIZON_5D_SECONDS,
    HORIZON_30D_SECONDS,
    RiskOutput,
)
from endure.protocol.risk_miner import BASELINE_BPS_VALUES, LatestPoolObservation
from endure.scoring.market_data import AlphaPriceSeries, AlphaPriceSnapshot


def _history() -> AlphaPriceSeries:
    snapshots = tuple(
        AlphaPriceSnapshot(
            netuid=8,
            block=index * 600,
            price_tao_per_alpha=Decimal(10_000 - index * 15) / Decimal(10_000),
            tao_reserve_rao=1_000_000_000_000 - index * 1_000_000_000,
        )
        for index in range(361)
    )
    return AlphaPriceSeries(source="test-history", netuid=8, snapshots=snapshots)


def _outputs(bundle, horizon: int):  # noqa: ANN001, ANN202
    return {
        item.output: item
        for item in bundle.assets[0].outputs
        if item.horizon_seconds == horizon
    }


def test_adaptive_bundle_covers_every_output_and_horizon() -> None:
    history = _history()
    latest = history.latest_pool_observation()

    bundle = adaptive_risk_bundle(
        round_id="2026-09-15",
        netuids=(8,),
        latest_observation=lambda _netuid: latest,
        recent_price_series=lambda _netuid, _seconds: history,
    )

    assert len(bundle.assets) == 1
    assert len(bundle.assets[0].outputs) == 8
    for horizon in (HORIZON_5D_SECONDS, HORIZON_30D_SECONDS):
        outputs = _outputs(bundle, horizon)
        assert set(outputs) == set(RiskOutput)
        assert all(
            ADAPTIVE_REASON_CODE in item.reason_codes for item in outputs.values()
        )


def test_adaptive_bundle_biases_forecasts_toward_safer_direction() -> None:
    history = _history()
    latest = history.latest_pool_observation()

    bundle = adaptive_risk_bundle(
        round_id="2026-09-15",
        netuids=(8,),
        latest_observation=lambda _netuid: latest,
        recent_price_series=lambda _netuid, _seconds: history,
    )

    outputs_30d = _outputs(bundle, HORIZON_30D_SECONDS)
    assert (
        outputs_30d[RiskOutput.MAX_DRAWDOWN].value
        >= BASELINE_BPS_VALUES[(RiskOutput.MAX_DRAWDOWN, HORIZON_30D_SECONDS)]
    )
    assert outputs_30d[RiskOutput.TWAP_PRICE].value < latest.price_rao
    assert outputs_30d[RiskOutput.LIQUIDITY_DEPTH].value < latest.tao_reserve_rao


def test_adaptive_bundle_falls_back_when_history_is_missing() -> None:
    observation = LatestPoolObservation(
        price_rao=25_000_000, tao_reserve_rao=4_000_000_000
    )

    bundle = adaptive_risk_bundle(
        round_id="2026-09-15",
        netuids=(8,),
        latest_observation=lambda _netuid: observation,
        recent_price_series=lambda _netuid, _seconds: None,
    )

    outputs_5d = _outputs(bundle, HORIZON_5D_SECONDS)
    assert (
        outputs_5d[RiskOutput.MAX_DRAWDOWN].value
        == BASELINE_BPS_VALUES[(RiskOutput.MAX_DRAWDOWN, HORIZON_5D_SECONDS)]
    )
    assert outputs_5d[RiskOutput.TWAP_PRICE].value == observation.price_rao
    assert all(
        item.reason_codes == ("baseline_persistence",) for item in outputs_5d.values()
    )
