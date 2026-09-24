"""Operator-side history access layered over Endure's public provider."""

from __future__ import annotations

from collections.abc import Callable

from endure.live.alpha_market_data import LiveAlphaPriceProvider
from endure.scoring.assessment_orchestrator import ResolutionDeadlineExceeded
from endure.scoring.market_data import (
    AlphaMarketDataError,
    AlphaPriceSeries,
    FixtureAlphaPriceProvider,
    ResolutionWindow,
)
from endure.scoring.risk.observables import (
    BLOCK_SECONDS,
    CANONICAL_ALPHA_SNAPSHOT_CADENCE_BLOCKS,
)


def _horizon_blocks(lookback_seconds: int) -> int:
    blocks = lookback_seconds // BLOCK_SECONDS
    if lookback_seconds <= 0 or blocks * BLOCK_SECONDS != lookback_seconds:
        raise AlphaMarketDataError(
            "lookback_seconds must be positive and align to block cadence"
        )
    return blocks


class AdaptiveLiveAlphaPriceProvider(LiveAlphaPriceProvider):
    """Expose a trailing window without changing the versioned Endure provider."""

    history_deadline_exceeded_fn: Callable[[], bool] | None = None

    def recent_price_series(
        self, netuid: int, lookback_seconds: int
    ) -> AlphaPriceSeries | None:
        deadline = self.history_deadline_exceeded_fn
        if deadline is not None and deadline():
            raise ResolutionDeadlineExceeded("miner history fetch deadline reached")
        horizon_blocks = _horizon_blocks(lookback_seconds)
        # Align forecasts to a stable grid. Daily head blocks vary slightly,
        # but a fixed grid lets the provider reuse almost all cached snapshots.
        end_block = (
            self._current_block() // CANONICAL_ALPHA_SNAPSHOT_CADENCE_BLOCKS
        ) * CANONICAL_ALPHA_SNAPSHOT_CADENCE_BLOCKS
        if end_block < horizon_blocks:
            return None
        prior_deadline = self._deadline_exceeded_fn
        self._deadline_exceeded_fn = deadline
        try:
            return self.price_series(
                netuid,
                window=ResolutionWindow(
                    start_block=end_block - horizon_blocks,
                    horizon_blocks=horizon_blocks,
                ),
            )
        finally:
            # The current-head fallback must still work after history times out.
            self._deadline_exceeded_fn = prior_deadline


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
