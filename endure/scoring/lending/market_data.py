"""Dormant Forge lending market data over the shared Alpha provider (risk scope spec §Market-data extension)."""

from __future__ import annotations

from endure.assessment.schemas.forge_lending import (
    COLLATERAL_FACTOR_LIQUIDATION_BUFFER_BPS,
)
from endure.scoring.lending.observables import (
    MIN_COLLATERAL_FACTOR_PRICE_POINTS,
    collateral_factor_optimal_bps,
)
from endure.scoring.market_data import (
    MAX_ALPHA_PRICE_PAYLOAD_BYTES,
    SUBTENSOR_RESERVE_PRICE_SOURCE,
    AlphaMarketDataError,
    AlphaPriceProvider,
    AlphaPriceSeries,
    AlphaPriceSnapshot,
    FixtureAlphaPriceProvider,
    ResolutionWindow,
    canonical_alpha_price_payload_bytes,
    parse_alpha_price_payload,
    recorded_mainnet_fixture_provider,
)

__all__ = (
    "MAX_ALPHA_PRICE_PAYLOAD_BYTES",
    "SUBTENSOR_RESERVE_PRICE_SOURCE",
    "AlphaMarketDataError",
    "AlphaPriceProvider",
    "AlphaPriceSeries",
    "AlphaPriceSnapshot",
    "FixtureAlphaPriceProvider",
    "ResolutionWindow",
    "canonical_alpha_price_payload_bytes",
    "collateral_factor_target_for_netuid",
    "parse_alpha_price_payload",
    "recorded_mainnet_fixture_provider",
)


def collateral_factor_target_for_netuid(
    provider: AlphaPriceProvider,
    *,
    netuid: int,
    window: ResolutionWindow,
    liquidation_buffer_bps: int = COLLATERAL_FACTOR_LIQUIDATION_BUFFER_BPS,
) -> int | None:
    """Return the dormant lending CF target for a netuid, or skip absent data."""
    series = provider.price_series(netuid, window=window)
    if series is None:
        return None
    if series.netuid != netuid:
        raise AlphaMarketDataError(
            f"provider returned series for netuid {series.netuid}, expected {netuid}"
        )
    if len(series.snapshots) < MIN_COLLATERAL_FACTOR_PRICE_POINTS:
        return None
    return collateral_factor_optimal_bps(
        series.prices, liquidation_buffer_bps=liquidation_buffer_bps
    )
