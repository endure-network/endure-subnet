"""Operator-side history access layered over Endure's public provider."""

from __future__ import annotations

from endure.live.alpha_market_data import LiveAlphaPriceProvider
from endure.scoring.market_data import (
    AlphaMarketDataError,
    AlphaPriceSeries,
    FixtureAlphaPriceProvider,
    ResolutionWindow,
)
from endure.scoring.risk.observables import BLOCK_SECONDS


def _horizon_blocks(lookback_seconds: int) -> int:
    blocks = lookback_seconds // BLOCK_SECONDS
    if lookback_seconds <= 0 or blocks * BLOCK_SECONDS != lookback_seconds:
        raise AlphaMarketDataError(
            "lookback_seconds must be positive and align to block cadence"
        )
    return blocks


class AdaptiveLiveAlphaPriceProvider(LiveAlphaPriceProvider):
    """Expose a trailing window without changing the versioned Endure provider."""

    def recent_price_series(
        self, netuid: int, lookback_seconds: int
    ) -> AlphaPriceSeries | None:
        horizon_blocks = _horizon_blocks(lookback_seconds)
        end_block = self._current_block()
        if end_block < horizon_blocks:
            return None
        return self.price_series(
            netuid,
            window=ResolutionWindow(
                start_block=end_block - horizon_blocks,
                horizon_blocks=horizon_blocks,
            ),
        )


def fixture_recent_price_series(
    provider: FixtureAlphaPriceProvider, netuid: int, lookback_seconds: int
) -> AlphaPriceSeries | None:
    """Clip deterministic mock history to the requested trailing duration."""
    horizon_blocks = _horizon_blocks(lookback_seconds)
    series = provider.series_by_netuid.get(netuid)
    if series is None:
        return None
    end_block = series.snapshots[-1].block
    snapshots = tuple(
        snapshot
        for snapshot in series.snapshots
        if snapshot.block > end_block - horizon_blocks
    )
    if not snapshots:
        return None
    return AlphaPriceSeries(
        source=f"{series.source}_recent_{lookback_seconds}",
        netuid=netuid,
        snapshots=snapshots,
    )
