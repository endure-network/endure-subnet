"""History-aware Alpha Risk strategy for the Endure subnet.

The public reference miner intentionally persists the latest pool observation
and uses fixed risk constants.  This module keeps the same signed wire contract
while deriving forecasts from trailing market history.  Every historical
forecast is biased toward the conservative side of Alpha Risk V1's asymmetric
score. Missing or unusable history falls back per horizon to the public
baseline, so an archive gap does not turn into a missing-coordinate penalty.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final

from endure.assessment.schemas.subnet_alpha_risk import (
    HORIZON_5D_SECONDS,
    HORIZON_30D_SECONDS,
    RISK_HORIZONS,
    RISK_SCHEMA_ID,
    RISK_SPECS_BY_OUTPUT,
    RiskAssetSubmission,
    RiskOutput,
    RiskOutputValue,
    RiskSubmissionBundle,
)
from endure.protocol.bundles import AssembledSubmission, assemble_bundle
from endure.protocol.risk_miner import (
    BASELINE_BPS_VALUES,
    BASELINE_REASON_CODE,
    LatestPoolObservation,
    LatestPoolObservationProvider,
)
from endure.scoring.assessment_orchestrator import ResolutionDeadlineExceeded
from endure.scoring.market_data import (
    AlphaMarketDataError,
    AlphaPriceSeries,
    AlphaPriceSnapshot,
)
from endure.scoring.risk.observables import (
    horizon_seconds_to_blocks,
    liquidity_depth_rao,
    max_drawdown_bps,
    realized_volatility_bps,
    should_void_realized_window,
    twap_price_rao,
)

ADAPTIVE_REASON_CODE: Final = "adaptive_history_v1"
CONSERVATIVE_REASON_CODE: Final = "asymmetric_safety_margin"

# These margins deliberately lean away from Alpha Risk V1's dangerous miss:
# risk outputs are raised, while price and depth outputs are lowered.  They are
# policy, not protocol constants, and can be recalibrated from scored rounds.
RISK_MULTIPLIER_BPS: Final = {
    HORIZON_5D_SECONDS: 11_500,
    HORIZON_30D_SECONDS: 12_500,
}
PRICE_MULTIPLIER_BPS: Final = {
    HORIZON_5D_SECONDS: 9_800,
    HORIZON_30D_SECONDS: 9_500,
}
DEPTH_MULTIPLIER_BPS: Final = {
    HORIZON_5D_SECONDS: 9_500,
    HORIZON_30D_SECONDS: 9_000,
}

RecentPriceSeriesProvider = Callable[[int, int], AlphaPriceSeries | None]


def _scaled(value: int, multiplier_bps: int) -> int:
    return int(
        (Decimal(value) * Decimal(multiplier_bps) / Decimal(10_000)).quantize(
            Decimal("1"), rounding=ROUND_HALF_EVEN
        )
    )


def _bounded(output: RiskOutput, value: int) -> int:
    spec = RISK_SPECS_BY_OUTPUT[output]
    bounded = max(spec.min_value, value)
    return bounded if spec.max_value is None else min(spec.max_value, bounded)


def _output_value(
    output: RiskOutput,
    value: int,
    horizon: int,
    *,
    reason_codes: tuple[str, ...],
) -> RiskOutputValue:
    spec = RISK_SPECS_BY_OUTPUT[output]
    return RiskOutputValue(
        output=output,
        value=_bounded(output, value),
        confidence_bps=spec.confidence_floor_bps,
        reason_codes=reason_codes,
        horizon_seconds=horizon,
        unit=spec.unit,
    )


def _baseline_horizon_outputs(
    observation: LatestPoolObservation, horizon: int
) -> tuple[RiskOutputValue, ...]:
    return (
        _output_value(
            RiskOutput.MAX_DRAWDOWN,
            BASELINE_BPS_VALUES[(RiskOutput.MAX_DRAWDOWN, horizon)],
            horizon,
            reason_codes=(BASELINE_REASON_CODE,),
        ),
        _output_value(
            RiskOutput.REALIZED_VOLATILITY,
            BASELINE_BPS_VALUES[(RiskOutput.REALIZED_VOLATILITY, horizon)],
            horizon,
            reason_codes=(BASELINE_REASON_CODE,),
        ),
        _output_value(
            RiskOutput.TWAP_PRICE,
            observation.price_rao,
            horizon,
            reason_codes=(BASELINE_REASON_CODE,),
        ),
        _output_value(
            RiskOutput.LIQUIDITY_DEPTH,
            observation.tao_reserve_rao,
            horizon,
            reason_codes=(BASELINE_REASON_CODE,),
        ),
    )


def _window_for_horizon(
    series: AlphaPriceSeries, horizon: int
) -> tuple[int, tuple[AlphaPriceSnapshot, ...]]:
    horizon_blocks = horizon_seconds_to_blocks(horizon)
    window_start = max(0, series.snapshots[-1].block - horizon_blocks)
    snapshots = tuple(
        snapshot for snapshot in series.snapshots if snapshot.block > window_start
    )
    if should_void_realized_window(snapshots, horizon_blocks=horizon_blocks):
        raise AlphaMarketDataError("historical window is too sparse")
    return window_start, snapshots


def _adaptive_horizon_outputs(
    *,
    observation: LatestPoolObservation,
    series: AlphaPriceSeries,
    horizon: int,
) -> tuple[RiskOutputValue, ...]:
    window_start, snapshots = _window_for_horizon(series, horizon)
    historical_drawdown = max_drawdown_bps(snapshots)
    historical_volatility = realized_volatility_bps(snapshots)
    historical_twap = twap_price_rao(snapshots, window_start_block=window_start)
    historical_depth = liquidity_depth_rao(snapshots, window_start_block=window_start)

    risk_multiplier = RISK_MULTIPLIER_BPS[horizon]
    drawdown = max(
        BASELINE_BPS_VALUES[(RiskOutput.MAX_DRAWDOWN, horizon)],
        _scaled(historical_drawdown, risk_multiplier),
    )
    volatility = max(
        BASELINE_BPS_VALUES[(RiskOutput.REALIZED_VOLATILITY, horizon)],
        _scaled(historical_volatility, risk_multiplier),
    )
    price = _scaled(
        min(observation.price_rao, historical_twap), PRICE_MULTIPLIER_BPS[horizon]
    )
    depth = _scaled(
        min(observation.tao_reserve_rao, historical_depth),
        DEPTH_MULTIPLIER_BPS[horizon],
    )
    reason_codes = (
        ADAPTIVE_REASON_CODE,
        f"historical_{horizon // 86_400}d",
        CONSERVATIVE_REASON_CODE,
    )
    return (
        _output_value(
            RiskOutput.MAX_DRAWDOWN,
            drawdown,
            horizon,
            reason_codes=reason_codes,
        ),
        _output_value(
            RiskOutput.REALIZED_VOLATILITY,
            volatility,
            horizon,
            reason_codes=reason_codes,
        ),
        _output_value(
            RiskOutput.TWAP_PRICE,
            price,
            horizon,
            reason_codes=reason_codes,
        ),
        _output_value(
            RiskOutput.LIQUIDITY_DEPTH,
            depth,
            horizon,
            reason_codes=reason_codes,
        ),
    )


def adaptive_risk_bundle(
    *,
    round_id: str,
    netuids: Sequence[int],
    latest_observation: LatestPoolObservationProvider,
    recent_price_series: RecentPriceSeriesProvider,
) -> RiskSubmissionBundle:
    """Build one full-coverage history-aware Alpha Risk bundle."""
    assets: list[RiskAssetSubmission] = []
    for netuid in sorted(set(netuids)):
        try:
            series = recent_price_series(netuid, HORIZON_30D_SECONDS)
        except (
            AlphaMarketDataError,
            ResolutionDeadlineExceeded,
            ConnectionError,
            LookupError,
        ):
            series = None
        try:
            observation = latest_observation(netuid)
        except (
            AlphaMarketDataError,
            ResolutionDeadlineExceeded,
            ConnectionError,
            LookupError,
        ):
            observation = None
        if observation is None and series is not None:
            observation = series.latest_pool_observation()
        if observation is None:
            continue

        outputs: list[RiskOutputValue] = []
        for horizon in RISK_HORIZONS:
            if series is None:
                outputs.extend(_baseline_horizon_outputs(observation, horizon))
                continue
            try:
                outputs.extend(
                    _adaptive_horizon_outputs(
                        observation=observation,
                        series=series,
                        horizon=horizon,
                    )
                )
            except AlphaMarketDataError:
                outputs.extend(_baseline_horizon_outputs(observation, horizon))
        assets.append(RiskAssetSubmission(netuid=netuid, outputs=tuple(outputs)))

    return RiskSubmissionBundle(
        round_id=round_id, schema_id=RISK_SCHEMA_ID, assets=tuple(assets)
    )


@dataclass(frozen=True, slots=True)
class AdaptiveRiskAssembler:
    """Assemble and hash a history-aware Alpha Risk submission."""

    netuids: tuple[int, ...]
    miner_hotkey: str
    latest_observation: LatestPoolObservationProvider
    recent_price_series: RecentPriceSeriesProvider

    @property
    def schema_id(self) -> str:
        return RISK_SCHEMA_ID

    def __call__(self, round_id: str) -> AssembledSubmission:
        return assemble_bundle(
            adaptive_risk_bundle(
                round_id=round_id,
                netuids=self.netuids,
                latest_observation=self.latest_observation,
                recent_price_series=self.recent_price_series,
            ),
            miner_hotkey=self.miner_hotkey,
        )
