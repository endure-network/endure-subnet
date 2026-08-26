"""Alpha Risk V1 realized-value estimators (risk scope spec §Realized-value estimators)."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Final

from endure.scoring.context import TR_CONTEXT
from endure.scoring.market_data import AlphaMarketDataError, AlphaPriceSnapshot

BLOCK_SECONDS: Final = 12
SECONDS_PER_YEAR: Final = 365 * 24 * 60 * 60
CANONICAL_ALPHA_SNAPSHOT_CADENCE_SECONDS: Final = 2 * 60 * 60
CANONICAL_ALPHA_SNAPSHOT_CADENCE_BLOCKS: Final = (
    CANONICAL_ALPHA_SNAPSHOT_CADENCE_SECONDS // BLOCK_SECONDS
)
MIN_REALIZED_WINDOW_SNAPSHOTS: Final = 20
MIN_VOLATILITY_SNAPSHOTS: Final = 2
MIN_VOLATILITY_EXACT_CADENCE_RETURNS: Final = MIN_REALIZED_WINDOW_SNAPSHOTS // 2
MIN_REALIZED_WINDOW_COVERAGE_NUMERATOR: Final = 4
MIN_REALIZED_WINDOW_COVERAGE_DENOMINATOR: Final = 5
RAO_PER_TAO: Final = Decimal(1_000_000_000)


def horizon_seconds_to_blocks(horizon_seconds: int) -> int:
    """Convert risk horizons to block windows using Bittensor's 12s block cadence."""
    if horizon_seconds <= 0:
        raise AlphaMarketDataError("horizon_seconds must be positive")
    if horizon_seconds % BLOCK_SECONDS != 0:
        raise AlphaMarketDataError("horizon_seconds must align to 12s blocks")
    return horizon_seconds // BLOCK_SECONDS


def filter_snapshots_for_window(
    series: Sequence[AlphaPriceSnapshot],
    *,
    window_start_block: int,
    horizon_blocks: int,
) -> tuple[AlphaPriceSnapshot, ...]:
    """Return snapshots in the pure half-open window from risk scope §Realized-value estimators."""
    if horizon_blocks <= 0:
        raise AlphaMarketDataError("horizon_blocks must be positive")
    window_end_block = window_start_block + horizon_blocks
    return tuple(
        snapshot
        for snapshot in series
        if window_start_block < snapshot.block <= window_end_block
    )


def should_void_realized_window(
    series: Sequence[AlphaPriceSnapshot], *, horizon_blocks: int
) -> bool:
    """Apply risk scope §Voiding independently for one netuid/horizon cell."""
    if len(series) < MIN_REALIZED_WINDOW_SNAPSHOTS:
        return True
    span = series[-1].block - series[0].block
    return span * MIN_REALIZED_WINDOW_COVERAGE_DENOMINATOR < (
        horizon_blocks * MIN_REALIZED_WINDOW_COVERAGE_NUMERATOR
    )


def max_drawdown_bps(series: Sequence[AlphaPriceSnapshot]) -> int:
    """O(n) peak-to-trough drawdown in bps, per risk scope §Realized-value estimators."""
    if not series:
        raise AlphaMarketDataError("drawdown series must not be empty")
    with localcontext(TR_CONTEXT):
        peak = series[0].price_tao_per_alpha
        worst = Decimal(0)
        for snapshot in series:
            price = snapshot.price_tao_per_alpha
            peak = max(peak, price)
            drawdown = Decimal(1) - price / peak
            worst = max(worst, drawdown)
        return _quantize_int(worst * Decimal(10_000))


def realized_volatility_bps(
    series: Sequence[AlphaPriceSnapshot],
    cadence_blocks: int = CANONICAL_ALPHA_SNAPSHOT_CADENCE_BLOCKS,
) -> int:
    """Annualized population stdev of Decimal log returns, risk scope §Realized-value estimators."""
    if len(series) < MIN_VOLATILITY_SNAPSHOTS:
        raise AlphaMarketDataError("volatility requires enough exact-cadence pairs")
    if cadence_blocks <= 0:
        raise AlphaMarketDataError("cadence_blocks must be positive")
    with localcontext(TR_CONTEXT):
        returns = tuple(
            (current.price_tao_per_alpha / previous.price_tao_per_alpha).ln()
            for previous, current in zip(series, series[1:])
            if current.block - previous.block == cadence_blocks
        )
        if len(returns) < MIN_VOLATILITY_EXACT_CADENCE_RETURNS:
            raise AlphaMarketDataError("volatility requires enough exact-cadence pairs")
        mean = sum(returns, Decimal(0)) / Decimal(len(returns))
        variance = sum((item - mean) ** 2 for item in returns) / Decimal(len(returns))
        periods_per_year = Decimal(SECONDS_PER_YEAR) / Decimal(
            cadence_blocks * BLOCK_SECONDS
        )
        return _quantize_int(
            variance.sqrt() * periods_per_year.sqrt() * Decimal(10_000)
        )


def twap_price_rao(
    series: Sequence[AlphaPriceSnapshot], *, window_start_block: int
) -> int:
    """Block-weighted mean price in RAO using risk scope §Realized-value estimators."""
    return _block_weighted_decimal(
        series,
        window_start_block=window_start_block,
        values=tuple(snapshot.price_tao_per_alpha * RAO_PER_TAO for snapshot in series),
    )


def liquidity_depth_rao(
    series: Sequence[AlphaPriceSnapshot], *, window_start_block: int
) -> int:
    """Block-weighted mean TAO reserve in RAO using risk scope §Realized-value estimators."""
    return _block_weighted_decimal(
        series,
        window_start_block=window_start_block,
        values=tuple(Decimal(snapshot.tao_reserve_rao) for snapshot in series),
    )


def _block_weighted_decimal(
    series: Sequence[AlphaPriceSnapshot],
    *,
    window_start_block: int,
    values: Sequence[Decimal],
) -> int:
    if not series:
        raise AlphaMarketDataError("weighted series must not be empty")
    if len(series) != len(values):
        raise AlphaMarketDataError("weighted values must match snapshots")
    if series[0].block <= window_start_block:
        raise AlphaMarketDataError(
            "first weighted snapshot must follow window_start_block"
        )
    with localcontext(TR_CONTEXT):
        weighted_sum = Decimal(0)
        total_weight = 0
        previous_block = window_start_block
        for index, snapshot in enumerate(series):
            span = snapshot.block - previous_block
            if span <= 0:
                raise AlphaMarketDataError("price snapshots must be ascending by block")
            weight = min(span, CANONICAL_ALPHA_SNAPSHOT_CADENCE_BLOCKS)
            weighted_sum += values[index] * Decimal(weight)
            total_weight += weight
            previous_block = snapshot.block
        return _quantize_int(weighted_sum / Decimal(total_weight))


def _quantize_int(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
