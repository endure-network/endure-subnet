"""Schema-neutral Alpha pool market data (risk scope spec §Market-data extension)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Final, Protocol

from endure.protocol.canonical import canonical_bundle_bytes
from endure.protocol.risk_miner import LatestPoolObservation
from endure.scoring.recorded_fixtures.alpha_mainnet import RECORDED_ALPHA_MAINNET_ROWS

MAX_ALPHA_PRICE_PAYLOAD_BYTES: Final = 64 * 1024
PRICE_QUANTUM: Final = Decimal("0.000000001")
SUBTENSOR_RESERVE_PRICE_SOURCE: Final = "subtensor_archive_subnettao_subnetalpha_v1"


class AlphaMarketDataError(ValueError):
    """Raised when Alpha pool market data is missing or unusable."""


class AlphaMarketDataUnavailable(AlphaMarketDataError):
    """Raised when an archive failure prevents a definitive Alpha data verdict."""


@dataclass(frozen=True, slots=True)
class ResolutionWindow:
    start_block: int
    horizon_blocks: int

    def __post_init__(self) -> None:
        if self.start_block < 0:
            raise AlphaMarketDataError("start_block must be non-negative")
        if self.horizon_blocks <= 0:
            raise AlphaMarketDataError("horizon_blocks must be positive")

    @property
    def end_block(self) -> int:
        return self.start_block + self.horizon_blocks


class AlphaPriceProvider(Protocol):
    """Provider interface for validated Alpha pool series."""

    def price_series(
        self, netuid: int, *, window: ResolutionWindow
    ) -> AlphaPriceSeries | None:
        """Return a validated pool series, ``None`` for a definitive gap, or raise."""


@dataclass(frozen=True, slots=True)
class AlphaPriceSnapshot:
    """One price/reserve snapshot for a subnet Alpha pool."""

    netuid: int
    block: int
    price_tao_per_alpha: Decimal
    tao_reserve_rao: int

    def __post_init__(self) -> None:
        if self.netuid < 0:
            raise AlphaMarketDataError(f"netuid must be non-negative: {self.netuid}")
        if self.block < 0:
            raise AlphaMarketDataError(f"block must be non-negative: {self.block}")
        if not self.price_tao_per_alpha.is_finite() or self.price_tao_per_alpha <= 0:
            raise AlphaMarketDataError("price_tao_per_alpha must be finite positive")
        if self.tao_reserve_rao < 0:
            raise AlphaMarketDataError("tao_reserve_rao must be non-negative")

    @property
    def price_rao_per_alpha(self) -> int:
        return int(self.price_tao_per_alpha * Decimal(1_000_000_000))

    def latest_pool_observation(self) -> LatestPoolObservation:
        return LatestPoolObservation(
            price_rao=self.price_rao_per_alpha,
            tao_reserve_rao=self.tao_reserve_rao,
        )


@dataclass(frozen=True, slots=True)
class AlphaPriceSeries:
    """Validated pool series hashed over the canonical provider payload."""

    source: str
    netuid: int
    snapshots: tuple[AlphaPriceSnapshot, ...]

    def __post_init__(self) -> None:
        if not self.source:
            raise AlphaMarketDataError("source must be non-empty")
        if self.netuid < 0:
            raise AlphaMarketDataError(f"netuid must be non-negative: {self.netuid}")
        if not self.snapshots:
            raise AlphaMarketDataError(
                "price series must include at least one snapshot"
            )
        for snapshot in self.snapshots:
            if snapshot.netuid != self.netuid:
                raise AlphaMarketDataError(
                    "snapshot netuid does not match series netuid"
                )
        for previous, current in zip(self.snapshots, self.snapshots[1:]):
            if current.block <= previous.block:
                raise AlphaMarketDataError("price snapshots must be ascending by block")

    @property
    def prices(self) -> tuple[Decimal, ...]:
        return tuple(snapshot.price_tao_per_alpha for snapshot in self.snapshots)

    @property
    def canonical_payload(self) -> bytes:
        return canonical_alpha_price_payload_bytes(
            source=self.source, netuid=self.netuid, snapshots=self.snapshots
        )

    @property
    def payload_hash(self) -> str:
        """Sha256 hex digest of the canonical payload, for audit and replay."""
        return hashlib.sha256(self.canonical_payload).hexdigest()

    def latest_pool_observation(self) -> LatestPoolObservation:
        return self.snapshots[-1].latest_pool_observation()


@dataclass(frozen=True, slots=True)
class FixtureAlphaPriceProvider:
    """Deterministic fixture provider for pre-live market-data tests."""

    series_by_netuid: Mapping[int, AlphaPriceSeries]

    def price_series(
        self, netuid: int, *, window: ResolutionWindow
    ) -> AlphaPriceSeries | None:
        series = self.series_by_netuid.get(netuid)
        if series is None:
            return None
        snapshots = tuple(
            snapshot
            for snapshot in series.snapshots
            if window.start_block < snapshot.block <= window.end_block
        )
        if not snapshots:
            return None
        return AlphaPriceSeries(
            source=f"{series.source}_window_{window.start_block}_{window.end_block}",
            netuid=series.netuid,
            snapshots=snapshots,
        )

    def latest_pool_observation(self, netuid: int) -> LatestPoolObservation | None:
        series = self.series_by_netuid.get(netuid)
        if series is None:
            return None
        return series.latest_pool_observation()


def alpha_snapshot_from_reserves(
    *, netuid: int, block: int, tao_rao: int, alpha_rao: int
) -> AlphaPriceSnapshot:
    """Build the canonical reserve-derived snapshot used by fixtures and live data.

    The price derivation intentionally mirrors
    ``scripts.record_lending_market_fixtures``: ``SubnetTAO / SubnetAlphaIn`` as
    Decimal RAO reserves, quantized to 1e-9 TAO/Alpha with half-even rounding
    (risk scope spec §Market-data extension).
    """
    if tao_rao <= 0 or alpha_rao <= 0:
        raise AlphaMarketDataError(
            f"empty reserves for netuid {netuid} at block {block}"
        )
    price = (Decimal(tao_rao) / Decimal(alpha_rao)).quantize(
        PRICE_QUANTUM, rounding=ROUND_HALF_EVEN
    )
    return AlphaPriceSnapshot(
        netuid=netuid,
        block=block,
        price_tao_per_alpha=price,
        tao_reserve_rao=tao_rao,
    )


def parse_alpha_price_payload(
    payload: bytes, *, max_payload_bytes: int = MAX_ALPHA_PRICE_PAYLOAD_BYTES
) -> AlphaPriceSeries:
    """Parse a bounded canonical price/reserve payload into a validated series."""
    if len(payload) > max_payload_bytes:
        raise AlphaMarketDataError("price payload too large")
    try:
        parsed: object = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise AlphaMarketDataError("price payload must be valid JSON") from error
    if not isinstance(parsed, dict):
        raise AlphaMarketDataError("price payload must be an object")
    source = _required_str(parsed, "source")
    netuid = _required_int(parsed, "netuid")
    raw_snapshots = parsed.get("snapshots")
    if not isinstance(raw_snapshots, list):
        raise AlphaMarketDataError("snapshots must be a list")
    series = AlphaPriceSeries(
        source=source,
        netuid=netuid,
        snapshots=tuple(
            _parse_snapshot(raw_snapshot, netuid=netuid)
            for raw_snapshot in raw_snapshots
        ),
    )
    if series.canonical_payload != payload:
        raise AlphaMarketDataError("price payload must be canonical")
    return series


def canonical_alpha_price_payload_bytes(
    *, source: str, netuid: int, snapshots: Sequence[AlphaPriceSnapshot]
) -> bytes:
    """Serialize the canonical provider payload hashed for risk scope §Market-data extension."""
    return canonical_bundle_bytes(
        {
            "netuid": netuid,
            "snapshots": [
                {
                    "block": snapshot.block,
                    "price_tao_per_alpha": str(snapshot.price_tao_per_alpha),
                    "tao_reserve_rao": snapshot.tao_reserve_rao,
                }
                for snapshot in snapshots
            ],
            "source": source,
        }
    )


def recorded_mainnet_fixture_provider() -> FixtureAlphaPriceProvider:
    """Return real recorded mainnet price/reserve fixtures for netuids 44 and 8."""
    return FixtureAlphaPriceProvider(
        series_by_netuid={
            netuid: AlphaPriceSeries(
                source=(
                    f"{SUBTENSOR_RESERVE_PRICE_SOURCE}:netuid_{netuid}"
                    f"_recorded_{rows[0][0]}_{rows[-1][0]}"
                ),
                netuid=netuid,
                snapshots=tuple(
                    AlphaPriceSnapshot(
                        netuid=netuid,
                        block=block,
                        price_tao_per_alpha=Decimal(price),
                        tao_reserve_rao=tao_reserve_rao,
                    )
                    for block, price, tao_reserve_rao in rows
                ),
            )
            for netuid, rows in RECORDED_ALPHA_MAINNET_ROWS.items()
        }
    )


def _parse_snapshot(raw_snapshot: object, *, netuid: int) -> AlphaPriceSnapshot:
    if not isinstance(raw_snapshot, dict):
        raise AlphaMarketDataError("snapshot must be an object")
    return AlphaPriceSnapshot(
        netuid=netuid,
        block=_required_int(raw_snapshot, "block"),
        price_tao_per_alpha=_required_decimal(raw_snapshot, "price_tao_per_alpha"),
        tao_reserve_rao=_required_int(raw_snapshot, "tao_reserve_rao"),
    )


def _required_str(payload: dict[object, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise AlphaMarketDataError(f"{key} must be a non-empty string")
    return value


def _required_int(payload: dict[object, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise AlphaMarketDataError(f"{key} must be an integer")
    if value < 0:
        raise AlphaMarketDataError(f"{key} must be non-negative")
    return value


def _required_decimal(payload: dict[object, object], key: str) -> Decimal:
    value = payload.get(key)
    if not isinstance(value, str):
        raise AlphaMarketDataError(f"{key} must be a decimal string")
    try:
        return Decimal(value)
    except InvalidOperation as error:
        raise AlphaMarketDataError(f"{key} must be a decimal string") from error
