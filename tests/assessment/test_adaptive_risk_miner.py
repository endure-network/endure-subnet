from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from custom_miner.adaptive import (
    ADAPTIVE_REASON_CODE,
    adaptive_risk_bundle,
)
from custom_miner.entrypoint import _history_deadline_exceeded
from custom_miner.market_data import AdaptiveLiveAlphaPriceProvider
from endure.assessment.schemas.subnet_alpha_risk import (
    HORIZON_5D_SECONDS,
    HORIZON_30D_SECONDS,
    RiskOutput,
)
from endure.live.alpha_market_data import LiveAlphaPriceProviderConfig
from endure.protocol.risk_miner import BASELINE_BPS_VALUES, LatestPoolObservation
from endure.protocol.schedulers import FixedUtcScheduler
from endure.scoring.assessment_orchestrator import ResolutionDeadlineExceeded
from endure.scoring.market_data import (
    AlphaPriceSeries,
    AlphaPriceSnapshot,
    ResolutionWindow,
)
from endure.scoring.risk.observables import CANONICAL_ALPHA_SNAPSHOT_CADENCE_BLOCKS


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


def test_adaptive_bundle_uses_fresh_observation_with_history() -> None:
    history = _history()
    fresh = LatestPoolObservation(
        price_rao=100_000_000, tao_reserve_rao=100_000_000_000
    )

    bundle = adaptive_risk_bundle(
        round_id="2026-09-15",
        netuids=(8,),
        latest_observation=lambda _netuid: fresh,
        recent_price_series=lambda _netuid, _seconds: history,
    )

    outputs = _outputs(bundle, HORIZON_5D_SECONDS)
    assert outputs[RiskOutput.TWAP_PRICE].value == 98_000_000
    assert outputs[RiskOutput.LIQUIDITY_DEPTH].value == 95_000_000_000


def test_adaptive_bundle_uses_history_when_current_head_fails() -> None:
    history = _history()

    def unavailable(_netuid: int) -> LatestPoolObservation | None:
        raise ConnectionError("archive unavailable")

    bundle = adaptive_risk_bundle(
        round_id="2026-09-15",
        netuids=(8,),
        latest_observation=unavailable,
        recent_price_series=lambda _netuid, _seconds: history,
    )

    assert len(bundle.assets) == 1
    assert (
        ADAPTIVE_REASON_CODE
        in _outputs(bundle, HORIZON_5D_SECONDS)[RiskOutput.TWAP_PRICE].reason_codes
    )


def test_adaptive_bundle_preserves_coverage_after_history_deadline() -> None:
    latest = LatestPoolObservation(price_rao=25_000_000, tao_reserve_rao=4_000_000_000)

    def history_timed_out(_netuid: int, _seconds: int) -> AlphaPriceSeries | None:
        raise ResolutionDeadlineExceeded("too late")

    bundle = adaptive_risk_bundle(
        round_id="2026-09-15",
        netuids=(8,),
        latest_observation=lambda _netuid: latest,
        recent_price_series=history_timed_out,
    )

    assert len(bundle.assets[0].outputs) == 8
    assert all(
        item.reason_codes == ("baseline_persistence",)
        for item in bundle.assets[0].outputs
    )


def test_live_history_uses_stable_grid_and_restores_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AdaptiveLiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(endpoint="ws://127.0.0.1:9944")
    )
    heads = iter((216_725, 216_790))
    windows = []
    monkeypatch.setattr(provider, "_current_block", lambda: next(heads))
    monkeypatch.setattr(
        provider,
        "price_series",
        lambda _netuid, *, window: windows.append(window) or None,
    )
    provider.history_deadline_exceeded_fn = lambda: False

    provider.recent_price_series(8, HORIZON_30D_SECONDS)
    provider.recent_price_series(8, HORIZON_30D_SECONDS)

    assert windows[0] == windows[1]
    assert (
        windows[0].end_block
        == (216_725 // CANONICAL_ALPHA_SNAPSHOT_CADENCE_BLOCKS)
        * CANONICAL_ALPHA_SNAPSHOT_CADENCE_BLOCKS
    )
    assert provider._deadline_exceeded_fn is None


def test_live_history_deadline_does_not_block_latest_observation() -> None:
    provider = AdaptiveLiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(endpoint="ws://127.0.0.1:9944")
    )
    provider.history_deadline_exceeded_fn = lambda: True

    try:
        provider.recent_price_series(8, HORIZON_30D_SECONDS)
    except ResolutionDeadlineExceeded:
        pass
    else:
        raise AssertionError("history should stop at its deadline")

    assert provider._deadline_exceeded_fn is None


def test_live_history_restores_deadline_after_interrupted_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AdaptiveLiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(endpoint="ws://127.0.0.1:9944")
    )
    monkeypatch.setattr(provider, "_current_block", lambda: 216_600)
    provider.history_deadline_exceeded_fn = lambda: False

    def interrupted(_netuid: int, *, window: ResolutionWindow) -> None:
        raise ResolutionDeadlineExceeded("mid-fetch")

    monkeypatch.setattr(provider, "price_series", interrupted)
    try:
        provider.recent_price_series(8, HORIZON_30D_SECONDS)
    except ResolutionDeadlineExceeded:
        pass
    else:
        raise AssertionError("history fetch should stop")

    assert provider._deadline_exceeded_fn is None


def test_history_fallback_starts_before_commit_close() -> None:
    scheduler = FixedUtcScheduler(fetch_delay_seconds=0)

    assert not _history_deadline_exceeded(
        scheduler, datetime(2026, 9, 15, 18, 44, tzinfo=UTC)
    )
    assert _history_deadline_exceeded(
        scheduler, datetime(2026, 9, 15, 18, 45, tzinfo=UTC)
    )
