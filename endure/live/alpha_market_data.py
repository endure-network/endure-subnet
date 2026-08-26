"""Live Alpha pool market data from the Bittensor mainnet archive.

Stage-1 runs mainnet market data on a testnet/local subnet chain (risk scope
spec §Locked V1 decisions, decision 6). This provider therefore uses its own
``--endure.market_data_endpoint`` mainnet archive endpoint and never reuses
``--subtensor.*``, which points at the chain where the Endure subnet itself is
registered.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from threading import Condition
from typing import Final, Protocol

import bittensor as bt
from async_substrate_interface.errors import SubstrateRequestException
from async_substrate_interface.sync_substrate import SubstrateInterface

from endure.live.sleeping import sleep_decimal
from endure.protocol.risk_miner import LatestPoolObservation
from endure.scoring.market_data import (
    SUBTENSOR_RESERVE_PRICE_SOURCE,
    AlphaMarketDataError,
    AlphaMarketDataUnavailable,
    AlphaPriceSeries,
    AlphaPriceSnapshot,
    ResolutionWindow,
    alpha_snapshot_from_reserves,
)
from endure.scoring.risk.observables import (
    BLOCK_SECONDS,
    CANONICAL_ALPHA_SNAPSHOT_CADENCE_BLOCKS,
)
from endure.utils.logging import safe_error

MAINNET_ARCHIVE_ENDPOINT: Final = "wss://archive.chain.opentensor.ai:443"
LIVE_MARKET_DATA_MAX_ATTEMPTS: Final = 6
LIVE_MARKET_DATA_REQUEST_PAUSE_SECONDS: Final = Decimal("0.25")
LIVE_MARKET_DATA_REQUEST_TIMEOUT_SECONDS: Final = 10.0
LIVE_MARKET_DATA_TIMEOUT_WORKERS: Final = 1
LIVE_MARKET_DATA_MAX_ABANDONED_WORKERS: Final = 3
LIVE_MARKET_DATA_MAX_CONSECUTIVE_ARCHIVE_FAILURES: Final = 2
LIVE_MARKET_DATA_RATE_LIMIT_COOLDOWN_SECONDS: Final = 60.0
LIVE_MARKET_DATA_HEAD_CACHE_TTL_SECONDS: Final = 30.0
LIVE_MARKET_DATA_MAX_SERIES_CACHE_ENTRIES: Final = 64
LIVE_MARKET_DATA_SNAPSHOT_RETENTION_BLOCKS: Final = 30 * 24 * 60 * 60 // BLOCK_SECONDS

# The SDK's own retry substrate surfaces exhaustion as MaxRetriesExceeded
# (a SubstrateRequestException, plain Exception subclass) — observed live on
# 2026-07-07 when a transient DNS outage escaped the stdlib exception tuple
# and crashed resolution. Any failure at this boundary must mean "snapshot
# unavailable" (gap-skip / void downstream), never a crashed validator tick.
ARCHIVE_FETCH_FAILURES: Final = (
    ConnectionError,
    LookupError,
    OSError,
    RuntimeError,
    TimeoutError,
    SubstrateRequestException,
)

# A missing block is a legitimate gap, but these failures mean the archive
# connection itself is unavailable and can trigger the series outage breaker.
ARCHIVE_CONNECTION_FAILURES: Final = (
    ConnectionError,
    OSError,
    RuntimeError,
    TimeoutError,
    SubstrateRequestException,
)


_HTTP_TOO_MANY_REQUESTS: Final = 429


def _is_archive_rate_limited(error: BaseException) -> bool:
    # The archive proxy rejects the WebSocket handshake with HTTP 429 when the
    # source IP is throttled; websockets raises InvalidStatus (outside the
    # failure tuple), so match its status code or rendered "HTTP 429" message.
    response = getattr(error, "response", None)
    if getattr(response, "status_code", None) == _HTTP_TOO_MANY_REQUESTS:
        return True
    return str(_HTTP_TOO_MANY_REQUESTS) in str(error)


class SupportsInt(Protocol):
    def __int__(self) -> int: ...


class StorageValueLike(Protocol):
    @property
    def value(self) -> SupportsInt | None: ...


class DynamicInfoLike(Protocol):
    tao_in: SupportsInt
    alpha_in: SupportsInt


class AlphaSubnetInfoFetcher(Protocol):
    def subnet(self, *, netuid: int, block: int | None = None) -> DynamicInfoLike: ...

    def current_block(self) -> int: ...

    def finalized_block(self) -> int: ...

    def timestamp_at_block(self, block: int) -> int: ...


class ArchiveSubstrateLike(Protocol):
    def get_chain_finalised_head(self) -> str: ...

    def get_block_number(self, block_hash: str) -> int: ...

    def get_block_hash(self, block_id: int) -> str | None: ...

    def query(
        self,
        module: str,
        storage_function: str,
        params: list[str] | None = None,
        block_hash: str | None = None,
    ) -> StorageValueLike: ...


class Sleeper(Protocol):
    def __call__(self, seconds: Decimal, /) -> None: ...


class SubtensorLike(Protocol):
    def subnet(
        self, netuid: int, block: int | None = None
    ) -> DynamicInfoLike | None: ...

    def get_current_block(self) -> int: ...


@dataclass(frozen=True, slots=True)
class LiveAlphaPriceProviderConfig:
    endpoint: str = MAINNET_ARCHIVE_ENDPOINT
    request_pause_seconds: Decimal = LIVE_MARKET_DATA_REQUEST_PAUSE_SECONDS
    request_timeout_seconds: float = LIVE_MARKET_DATA_REQUEST_TIMEOUT_SECONDS
    max_attempts: int = LIVE_MARKET_DATA_MAX_ATTEMPTS


@dataclass(frozen=True, slots=True)
class _SnapshotFetchResult:
    snapshot: AlphaPriceSnapshot | None
    connection_available: bool


class BittensorSubnetInfoFetcher:
    """Thin SDK boundary around ``Subtensor.subnet`` for test injection.

    Single-caller only (validator tick loop or miner push loop): the
    ``_executor``/``_subtensor`` generation swap is unguarded, so add a lock
    before introducing any concurrent caller.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        request_timeout_seconds: float = LIVE_MARKET_DATA_REQUEST_TIMEOUT_SECONDS,
        subtensor: SubtensorLike | None = None,
        subtensor_factory: Callable[[], SubtensorLike] | None = None,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        if request_timeout_seconds <= 0:
            raise AlphaMarketDataError("request_timeout_seconds must be positive")
        self._request_timeout_seconds = request_timeout_seconds
        self._now_fn = now_fn
        self._cooldown_until = 0.0
        self._make_subtensor: Callable[[], SubtensorLike]
        if subtensor is not None:
            self._make_subtensor = lambda: subtensor
        else:
            self._make_subtensor = subtensor_factory or (
                lambda: bt.Subtensor(network=endpoint, archive_endpoints=[endpoint])
            )
        self._make_substrate: Callable[[], ArchiveSubstrateLike] = lambda: (
            SubstrateInterface(url=endpoint)
        )
        # Lazy: connect on first use inside the executor so the connection is
        # built and used on the same worker thread, never eagerly on the main
        # thread. Dropped with its executor on timeout (see _call_with_timeout).
        self._subtensor: SubtensorLike | None = None
        self._substrate: ArchiveSubstrateLike | None = None
        self._executor = self._new_executor()
        self._abandoned_workers = 0
        self._abandoned_workers_condition = Condition()

    def _new_executor(self) -> ThreadPoolExecutor:
        # One worker serializes access to the non-thread-safe SDK connection.
        # On timeout we abandon this worker/connection pair and rebuild both.
        return ThreadPoolExecutor(
            max_workers=LIVE_MARKET_DATA_TIMEOUT_WORKERS,
            thread_name_prefix="alpha-archive-timeout",
        )

    def subnet(self, *, netuid: int, block: int | None = None) -> DynamicInfoLike:
        subtensor = self._active_subtensor()
        if block is None:
            result = self._call_archive_operation(lambda: subtensor.subnet(netuid))
        else:
            result = self._call_archive_operation(
                lambda: subtensor.subnet(netuid, block=block)
            )
        if result is None:
            raise LookupError(f"archive returned no subnet info for netuid={netuid}")
        return result

    def current_block(self) -> int:
        subtensor = self._active_subtensor()
        return int(self._call_archive_operation(subtensor.get_current_block))

    def finalized_block(self) -> int:
        substrate = self._active_substrate()

        def operation() -> int:
            finalized_head = substrate.get_chain_finalised_head()
            return substrate.get_block_number(finalized_head)

        return int(self._call_archive_operation(operation))

    def timestamp_at_block(self, block: int) -> int:
        substrate = self._active_substrate()

        def operation() -> int:
            block_hash = substrate.get_block_hash(block)
            if block_hash is None:
                raise LookupError(f"archive missing block hash for block={block}")
            timestamp = substrate.query("Timestamp", "Now", block_hash=block_hash)
            if timestamp.value is None:
                raise LookupError(f"archive missing Timestamp.Now for block={block}")
            return int(timestamp.value)

        return int(self._call_archive_operation(operation))

    def _apply_rate_limit_cooldown(self, error: BaseException) -> None:
        if _is_archive_rate_limited(error):
            self._cooldown_until = (
                self._now_fn() + LIVE_MARKET_DATA_RATE_LIMIT_COOLDOWN_SECONDS
            )
            bt.logging.warning(
                "Alpha archive rate-limited (HTTP 429); backing off "
                f"{LIVE_MARKET_DATA_RATE_LIMIT_COOLDOWN_SECONDS:.0f}s"
            )

    def _active_subtensor(self) -> SubtensorLike:
        if self._now_fn() < self._cooldown_until:
            raise ConnectionError("archive rate-limited; cooling down")
        subtensor = self._subtensor
        if subtensor is None:
            try:
                subtensor = self._call_with_timeout(self._make_subtensor)
            except Exception as error:  # noqa: BLE001 — any reconnect failure voids
                self._apply_rate_limit_cooldown(error)
                if isinstance(error, ARCHIVE_FETCH_FAILURES):
                    raise
                raise ConnectionError("archive reconnect failed") from error
            self._subtensor = subtensor
        return subtensor

    def _active_substrate(self) -> ArchiveSubstrateLike:
        if self._now_fn() < self._cooldown_until:
            raise ConnectionError("archive rate-limited; cooling down")
        substrate = self._substrate
        if substrate is None:
            try:
                substrate = self._call_with_timeout(self._make_substrate)
            except Exception as error:  # noqa: BLE001 — any reconnect failure voids
                self._apply_rate_limit_cooldown(error)
                if isinstance(error, ARCHIVE_FETCH_FAILURES):
                    raise
                raise ConnectionError("archive reconnect failed") from error
            self._substrate = substrate
        return substrate

    def _call_archive_operation[T](self, operation: Callable[[], T]) -> T:
        try:
            return self._call_with_timeout(operation)
        except Exception as error:  # noqa: BLE001 — SDK failures must void snapshots
            # The cached SDK connection may be poisoned after any operation error.
            # A 429 additionally guards the next reconnect behind the cooldown.
            self._subtensor = None
            self._substrate = None
            self._apply_rate_limit_cooldown(error)
            if isinstance(error, ARCHIVE_FETCH_FAILURES):
                raise
            raise ConnectionError("archive operation failed") from error

    def _call_with_timeout[T](self, operation: Callable[[], T]) -> T:
        with self._abandoned_workers_condition:
            if self._abandoned_workers >= LIVE_MARKET_DATA_MAX_ABANDONED_WORKERS:
                raise ConnectionError("archive timed-out workers at capacity")
        future = self._executor.submit(operation)
        try:
            return future.result(timeout=self._request_timeout_seconds)
        except FuturesTimeoutError as error:
            # Python cannot cancel a running RPC. Replace its executor without
            # waiting, but cap still-live abandoned workers before admitting more
            # work so a permanently hung archive cannot grow threads unboundedly.
            future.cancel()
            old_executor = self._executor
            self._executor = self._new_executor()
            self._subtensor = None
            old_executor.shutdown(wait=False, cancel_futures=True)
            with self._abandoned_workers_condition:
                if not future.done():
                    self._abandoned_workers += 1
                    future.add_done_callback(self._release_abandoned_worker)
            raise TimeoutError("archive request timed out") from error

    def _release_abandoned_worker[T](self, _future: Future[T]) -> None:
        with self._abandoned_workers_condition:
            self._abandoned_workers -= 1
            self._abandoned_workers_condition.notify_all()


class LiveAlphaPriceProvider:
    """Archive-backed Alpha price/reserve provider for the R6 served runtime."""

    def __init__(
        self,
        *,
        config: LiveAlphaPriceProviderConfig,
        fetcher: AlphaSubnetInfoFetcher | None = None,
        sleep: Sleeper = sleep_decimal,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        if config.max_attempts <= 0:
            raise AlphaMarketDataError("max_attempts must be positive")
        if config.request_pause_seconds < Decimal(0):
            raise AlphaMarketDataError("request_pause_seconds must be non-negative")
        if config.request_timeout_seconds <= 0:
            raise AlphaMarketDataError("request_timeout_seconds must be positive")
        self._config = config
        self._fetcher = fetcher or BittensorSubnetInfoFetcher(
            config.endpoint, request_timeout_seconds=config.request_timeout_seconds
        )
        self._sleep = sleep
        self._now_fn = now_fn
        self._snapshots: dict[tuple[int, int], AlphaPriceSnapshot] = {}
        self._series: OrderedDict[tuple[int, ResolutionWindow], AlphaPriceSeries] = (
            OrderedDict()
        )
        self._head_cache: tuple[float, int] | None = None
        self._first_timestamp_blocks: dict[int, int] = {}
        self._last_timestamp_blocks: dict[int, int] = {}

    @property
    def endpoint(self) -> str:
        return self._config.endpoint

    def block_for_reveal_close(self, reveal_close: datetime, *, now: datetime) -> int:
        """Resolve the first finalized mainnet block at or after ``reveal_close``."""
        return self.first_finalized_block_at_or_after(reveal_close, now=now)

    def first_finalized_block_at_or_after(
        self, timestamp: datetime, *, now: datetime
    ) -> int:
        """Resolve the first finalized mainnet block at or after ``timestamp``."""
        _ = now  # Kept for validator call compatibility; chain state determines the block.
        timestamp_ms = _utc_timestamp_milliseconds(timestamp)
        cached = self._first_timestamp_blocks.get(timestamp_ms)
        if cached is not None:
            return cached

        finalized_block = self._with_retry(self._fetcher.finalized_block)
        finalized_timestamp = self._timestamp_at_block(finalized_block)
        if finalized_timestamp < timestamp_ms:
            raise AlphaMarketDataUnavailable(
                "timestamp is not yet covered by the finalized archive head"
            )
        if finalized_timestamp == timestamp_ms:
            self._first_timestamp_blocks[timestamp_ms] = finalized_block
            return finalized_block

        lower_block = 0
        upper_block = finalized_block
        while lower_block < upper_block:
            midpoint = lower_block + (upper_block - lower_block) // 2
            if self._timestamp_at_block(midpoint) >= timestamp_ms:
                upper_block = midpoint
            else:
                lower_block = midpoint + 1
        self._first_timestamp_blocks[timestamp_ms] = lower_block
        return lower_block

    def last_finalized_block_at_or_before(
        self, timestamp: datetime, *, now: datetime
    ) -> int:
        """Resolve the last finalized mainnet block at or before ``timestamp``."""
        _ = now  # Kept for validator call compatibility; chain state determines the block.
        timestamp_ms = _utc_timestamp_milliseconds(timestamp)
        cached = self._last_timestamp_blocks.get(timestamp_ms)
        if cached is not None:
            return cached

        finalized_block = self._with_retry(self._fetcher.finalized_block)
        finalized_timestamp = self._timestamp_at_block(finalized_block)
        if finalized_timestamp < timestamp_ms:
            raise AlphaMarketDataUnavailable(
                "timestamp is not yet covered by the finalized archive head"
            )
        if finalized_timestamp == timestamp_ms:
            self._last_timestamp_blocks[timestamp_ms] = finalized_block
            return finalized_block

        lower_block = 0
        upper_block = finalized_block
        while lower_block < upper_block:
            midpoint = lower_block + (upper_block - lower_block) // 2
            if self._timestamp_at_block(midpoint) > timestamp_ms:
                upper_block = midpoint
            else:
                lower_block = midpoint + 1
        last_block = lower_block - 1
        if last_block < 0:
            raise AlphaMarketDataUnavailable(
                "timestamp precedes the finalized archive history"
            )
        self._last_timestamp_blocks[timestamp_ms] = last_block
        return last_block

    def price_series(
        self, netuid: int, *, window: ResolutionWindow
    ) -> AlphaPriceSeries | None:
        key = (netuid, window)
        cached, current_block = self._current_block_for_series(key)
        if cached is not None:
            return cached

        snapshots: list[AlphaPriceSnapshot] = []
        skipped_future_block = False
        consecutive_archive_failures = 0
        archive_unavailable = False
        for block in _canonical_blocks(window):
            if block > current_block:
                skipped_future_block = True
            result = self._snapshot_at(
                netuid=netuid, block=block, current_block=current_block
            )
            snapshot = result.snapshot
            if snapshot is None:
                bt.logging.warning(
                    f"Alpha market-data snapshot skipped: netuid={netuid} block={block}"
                )
                if result.connection_available:
                    consecutive_archive_failures = 0
                else:
                    archive_unavailable = True
                    consecutive_archive_failures += 1
                    if (
                        consecutive_archive_failures
                        >= LIVE_MARKET_DATA_MAX_CONSECUTIVE_ARCHIVE_FAILURES
                    ):
                        break
                continue
            consecutive_archive_failures = 0
            snapshots.append(snapshot)
        if archive_unavailable:
            raise AlphaMarketDataUnavailable(
                f"archive data unavailable for netuid={netuid} window={window}"
            )
        if skipped_future_block:
            raise AlphaMarketDataUnavailable(
                f"archive head has not finalized netuid={netuid} window={window}"
            )
        if not snapshots:
            return None
        series = AlphaPriceSeries(
            source=(
                f"{SUBTENSOR_RESERVE_PRICE_SOURCE}:netuid_{netuid}"
                f"_live_{snapshots[0].block}_{snapshots[-1].block}"
            ),
            netuid=netuid,
            snapshots=tuple(snapshots),
        )
        self._prune_snapshot_cache(retain_from_block=window.start_block)
        self._series[key] = series
        self._series.move_to_end(key)
        while len(self._series) > LIVE_MARKET_DATA_MAX_SERIES_CACHE_ENTRIES:
            self._series.popitem(last=False)
        return series

    def _current_block_for_series(
        self, key: tuple[int, ResolutionWindow]
    ) -> tuple[AlphaPriceSeries | None, int]:
        try:
            cached = self._series.get(key)
            if cached is not None:
                current_block = self._current_block()
                if cached.snapshots[-1].block <= current_block:
                    self._series.move_to_end(key)
                    return cached, current_block
                del self._series[key]
            else:
                current_block = self._current_block()
        except ARCHIVE_FETCH_FAILURES as error:
            raise AlphaMarketDataUnavailable("archive head is unavailable") from error
        return None, current_block

    def latest_pool_observation(self, netuid: int) -> LatestPoolObservation | None:
        snapshot = self._fetch_snapshot(netuid=netuid, block=None).snapshot
        if snapshot is None:
            return None
        return snapshot.latest_pool_observation()

    def _snapshot_at(
        self, *, netuid: int, block: int, current_block: int
    ) -> _SnapshotFetchResult:
        # SDK 10.5.0 archive probe on 2026-07-07 returned current-head
        # DynamicInfo for future block requests instead of raising; skip them.
        if block > current_block:
            return _SnapshotFetchResult(snapshot=None, connection_available=True)
        key = (netuid, block)
        cached = self._snapshots.get(key)
        if cached is not None:
            return _SnapshotFetchResult(snapshot=cached, connection_available=True)
        result = self._fetch_snapshot(netuid=netuid, block=block)
        if result.snapshot is not None:
            self._snapshots[key] = result.snapshot
        return result

    def _fetch_snapshot(
        self, *, netuid: int, block: int | None
    ) -> _SnapshotFetchResult:
        try:
            info = self._with_retry(
                lambda: self._fetcher.subnet(netuid=netuid, block=block)
            )
            snapshot_block = (
                self._with_retry(self._fetcher.current_block)
                if block is None
                else block
            )
            return _SnapshotFetchResult(
                snapshot=alpha_snapshot_from_reserves(
                    netuid=netuid,
                    block=snapshot_block,
                    tao_rao=int(info.tao_in),
                    alpha_rao=int(info.alpha_in),
                ),
                connection_available=True,
            )
        except ARCHIVE_CONNECTION_FAILURES:
            return _SnapshotFetchResult(snapshot=None, connection_available=False)
        except AlphaMarketDataUnavailable:
            return _SnapshotFetchResult(snapshot=None, connection_available=False)
        except LookupError:
            return _SnapshotFetchResult(snapshot=None, connection_available=True)
        except AlphaMarketDataError:
            return _SnapshotFetchResult(snapshot=None, connection_available=True)

    def _with_retry[T](self, operation: Callable[[], T]) -> T:
        for attempt in range(1, self._config.max_attempts + 1):
            try:
                return operation()
            except ARCHIVE_FETCH_FAILURES as error:
                if attempt == self._config.max_attempts:
                    message = f"archive request failed: {safe_error(error)}"
                    if isinstance(error, LookupError):
                        raise LookupError(message) from None
                    raise ConnectionError(message) from None
                self._sleep(
                    self._config.request_pause_seconds * (Decimal(2) ** (attempt - 1))
                )
        raise AlphaMarketDataError("unreachable retry exhaustion")

    def _timestamp_at_block(self, block: int) -> int:
        return self._with_retry(lambda: self._fetcher.timestamp_at_block(block))

    def _current_block(self) -> int:
        now = self._now_fn()
        cached = self._head_cache
        if cached is not None:
            fetched_at, block = cached
            if now - fetched_at < LIVE_MARKET_DATA_HEAD_CACHE_TTL_SECONDS:
                return block
        block = self._with_retry(self._fetcher.current_block)
        self._head_cache = (now, block)
        return block

    def _prune_snapshot_cache(self, *, retain_from_block: int) -> None:
        cutoff = max(0, retain_from_block - LIVE_MARKET_DATA_SNAPSHOT_RETENTION_BLOCKS)
        self._snapshots = {
            key: snapshot
            for key, snapshot in self._snapshots.items()
            if key[1] >= cutoff
        }


def _canonical_blocks(window: ResolutionWindow) -> tuple[int, ...]:
    first_block = window.start_block + CANONICAL_ALPHA_SNAPSHOT_CADENCE_BLOCKS
    return tuple(
        range(
            first_block,
            window.end_block + CANONICAL_ALPHA_SNAPSHOT_CADENCE_BLOCKS,
            CANONICAL_ALPHA_SNAPSHOT_CADENCE_BLOCKS,
        )
    )


def _utc_timestamp_milliseconds(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AlphaMarketDataError("reveal_close must be timezone-aware")
    utc_value = value.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = utc_value - epoch
    return (
        elapsed.days * 86_400 + elapsed.seconds
    ) * 1_000 + elapsed.microseconds // 1_000
