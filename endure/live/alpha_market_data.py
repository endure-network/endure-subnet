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
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from threading import Condition, Event, Thread
from typing import Final, Protocol

import bittensor as bt
from async_substrate_interface.errors import (
    StateDiscardedError,
    SubstrateRequestException,
)
from async_substrate_interface.sync_substrate import SubstrateInterface

from endure.live.sleeping import sleep_decimal
from endure.protocol.consensus_policy import (
    MAINNET_GENESIS_HASH,
    normalize_genesis_hash,
)
from endure.protocol.risk_miner import LatestPoolObservation
from endure.scoring.assessment_orchestrator import ResolutionDeadlineExceeded
from endure.scoring.market_data import (
    SUBTENSOR_RESERVE_PRICE_SOURCE,
    AlphaMarketDataError,
    AlphaMarketDataUnavailable,
    AlphaPriceSeries,
    AlphaPriceSnapshot,
    ResolutionWindow,
    alpha_snapshot_from_reserves,
)
from endure.scoring.market_sampling import (
    ARCHIVE_FETCH_FAILURES,
    SCORING_ARCHIVE_ATTEMPTS,
    SNAPSHOT_FETCH_FAILURES,
    SeriesSampling,
    canonical_snapshot_blocks,
    first_block_at_or_after,
    last_block_at_or_before,
    require_archive_value,
    retry_exhausted_failure,
    snapshot_failure_is_outage,
)
from endure.scoring.risk.observables import BLOCK_SECONDS
from endure.utils.logging import safe_endpoint_label, safe_error

MAINNET_ARCHIVE_ENDPOINT: Final = "wss://archive.chain.opentensor.ai:443"
LIVE_MARKET_DATA_REQUEST_PAUSE_SECONDS: Final = Decimal("0.25")
LIVE_MARKET_DATA_REQUEST_TIMEOUT_SECONDS: Final = 10.0
LIVE_MARKET_DATA_TIMEOUT_WORKERS: Final = 1
LIVE_MARKET_DATA_MAX_ABANDONED_WORKERS: Final = 3
LIVE_MARKET_DATA_RATE_LIMIT_COOLDOWN_SECONDS: Final = 60.0
LIVE_MARKET_DATA_MIN_REQUEST_INTERVAL_SECONDS: Final = 0.5
LIVE_MARKET_DATA_HEAD_CACHE_TTL_SECONDS: Final = 30.0
LIVE_MARKET_DATA_MAX_SERIES_CACHE_ENTRIES: Final = 64
LIVE_MARKET_DATA_SNAPSHOT_RETENTION_BLOCKS: Final = 30 * 24 * 60 * 60 // BLOCK_SECONDS
LIVE_MARKET_DATA_ARCHIVE_PROBE_TIMEOUT_SECONDS: Final = 120.0
# The startup probe retries until its deadline instead of max_attempts, so a
# 429 cooldown at a coordinated restart is waited out; cap each backoff so the
# first attempt after the cooldown lands soon after it ends.
LIVE_MARKET_DATA_ARCHIVE_PROBE_MAX_BACKOFF_SECONDS: Final = Decimal("5")
LIVE_MARKET_DATA_ARCHIVE_LOOKBACK: Final = timedelta(days=30)
# Startup chain identity: a 429, a DNS blip or a devnet node still booting is
# retried with exponential backoff until the deadline; each attempt is bounded.
CHAIN_GENESIS_READ_DEADLINE_SECONDS: Final = 30.0
CHAIN_GENESIS_ATTEMPT_TIMEOUT_SECONDS: Final = 10.0
CHAIN_GENESIS_INITIAL_BACKOFF_SECONDS: Final = 0.5
CHAIN_GENESIS_MAX_BACKOFF_SECONDS: Final = 5.0

_HTTP_TOO_MANY_REQUESTS: Final = 429
_JSONRPC_RATE_LIMIT_CODE: Final = -32029


def _is_archive_rate_limited(error: BaseException) -> bool:
    # The archive proxy rejects the WebSocket handshake with HTTP 429 when the
    # source IP is throttled; websockets raises InvalidStatus (outside the
    # failure tuple), so match its status code or rendered "HTTP 429" message.
    response = getattr(error, "response", None)
    if getattr(response, "status_code", None) == _HTTP_TOO_MANY_REQUESTS:
        return True
    return str(_HTTP_TOO_MANY_REQUESTS) in str(error)


def _is_archive_request_rate_limited(error: BaseException) -> bool:
    # In-band throttling arrives as a well-formed JSON-RPC error (-32029 or a
    # textual rate-limit message): the connection is healthy, never void it.
    # Only SubstrateRequestException qualifies — a transport-level error whose
    # text mentions rate limiting must still void the connection.
    if not isinstance(error, SubstrateRequestException):
        return False
    if error.args:
        payload = error.args[0]
        if isinstance(payload, dict):
            detail = payload.get("error")
            if (
                isinstance(detail, dict)
                and detail.get("code") == _JSONRPC_RATE_LIMIT_CODE
            ):
                return True
    return "rate limit" in str(error).lower()


# A pruned node reports discarded historical state through the RPC error, not
# a LookupError. Only the startup probe reads it as missing history; scoring
# keeps treating it as an archive outage that defers the target.
_MISSING_HISTORY_MARKERS: Final = ("UnknownBlock", "State already discarded")


def _is_missing_history(error: BaseException) -> bool:
    # The SDK's retry substrate raises StateDiscardedError; a raw substrate
    # surfaces the node's "UnknownBlock: State already discarded" RPC error.
    if isinstance(error, LookupError | StateDiscardedError):
        return True
    return isinstance(error, SubstrateRequestException) and any(
        marker in str(error) for marker in _MISSING_HISTORY_MARKERS
    )


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

    def genesis_hash(self) -> str | None: ...


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
    max_attempts: int = SCORING_ARCHIVE_ATTEMPTS


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

    def __init__(  # noqa: PLR0913 — keyword-only test-injection seams
        self,
        endpoint: str,
        *,
        request_timeout_seconds: float = LIVE_MARKET_DATA_REQUEST_TIMEOUT_SECONDS,
        subtensor: SubtensorLike | None = None,
        subtensor_factory: Callable[[], SubtensorLike] | None = None,
        now_fn: Callable[[], float] = time.monotonic,
        min_request_interval_seconds: float = (
            LIVE_MARKET_DATA_MIN_REQUEST_INTERVAL_SECONDS
        ),
        pace_sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if request_timeout_seconds <= 0:
            raise AlphaMarketDataError("request_timeout_seconds must be positive")
        if min_request_interval_seconds < 0:
            raise AlphaMarketDataError(
                "min_request_interval_seconds must be non-negative"
            )
        self._request_timeout_seconds = request_timeout_seconds
        self._now_fn = now_fn
        self._min_request_interval_seconds = min_request_interval_seconds
        self._pace_sleep = pace_sleep
        self._last_request_at: float | None = None
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
        self._closed = False

    def _new_executor(self) -> ThreadPoolExecutor:
        # One worker serializes access to the non-thread-safe SDK connection.
        # On timeout we abandon this worker/connection pair and rebuild both.
        return ThreadPoolExecutor(
            max_workers=LIVE_MARKET_DATA_TIMEOUT_WORKERS,
            thread_name_prefix="alpha-archive-timeout",
        )

    def close(self) -> None:
        """Release connected clients on their worker, with a bounded wait."""
        if self._closed:
            return
        self._closed = True
        subtensor, substrate = self._subtensor, self._substrate
        self._subtensor = None
        self._substrate = None
        future = self._executor.submit(_close_archive_clients, subtensor, substrate)
        try:
            future.result(timeout=self._request_timeout_seconds)
        except Exception as error:  # noqa: BLE001 — preserve the readiness failure
            bt.logging.warning(f"archive cleanup incomplete: {type(error).__name__}")
        finally:
            self._executor.shutdown(wait=False)

    def genesis_hash(self) -> str | None:
        substrate = self._active_substrate()
        return self._call_archive_operation(lambda: substrate.get_block_hash(0))

    def subnet(self, *, netuid: int, block: int | None = None) -> DynamicInfoLike:
        subtensor = self._active_subtensor()
        if block is None:
            result = self._call_archive_operation(lambda: subtensor.subnet(netuid))
        else:
            result = self._call_archive_operation(
                lambda: subtensor.subnet(netuid, block=block)
            )
        return require_archive_value(
            result, f"returned no subnet info for netuid={netuid}"
        )

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
            block_hash = require_archive_value(
                substrate.get_block_hash(block),
                f"missing block hash for block={block}",
            )
            timestamp = substrate.query("Timestamp", "Now", block_hash=block_hash)
            return int(
                require_archive_value(
                    timestamp.value, f"missing Timestamp.Now for block={block}"
                )
            )

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
                subtensor = self._call_with_timeout(
                    self._make_subtensor, close_result_on_timeout=True
                )
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
                substrate = self._call_with_timeout(
                    self._make_substrate, close_result_on_timeout=True
                )
            except Exception as error:  # noqa: BLE001 — any reconnect failure voids
                self._apply_rate_limit_cooldown(error)
                if isinstance(error, ARCHIVE_FETCH_FAILURES):
                    raise
                raise ConnectionError("archive reconnect failed") from error
            self._substrate = substrate
        return substrate

    def _call_archive_operation[T](self, operation: Callable[[], T]) -> T:
        self._pace_request()
        try:
            return self._call_with_timeout(operation)
        except Exception as error:  # noqa: BLE001 — SDK failures must void snapshots
            # The cached SDK connection may be poisoned after any operation error.
            # A 429 additionally guards the next reconnect behind the cooldown.
            if not _is_archive_request_rate_limited(error):
                self._executor.submit(
                    _close_archive_clients, self._subtensor, self._substrate
                )
                self._subtensor = None
                self._substrate = None
            self._apply_rate_limit_cooldown(error)
            if isinstance(error, ARCHIVE_FETCH_FAILURES):
                raise
            raise ConnectionError("archive operation failed") from error

    def _pace_request(self) -> None:
        # The archive enforces a per-second request budget; spacing submissions
        # keeps a multi-thousand-query backfill under it instead of burning
        # retries on -32029 rejections.
        last = self._last_request_at
        if last is not None:
            wait = self._min_request_interval_seconds - (self._now_fn() - last)
            if wait > 0:
                self._pace_sleep(wait)
        self._last_request_at = self._now_fn()

    def _call_with_timeout[T](
        self, operation: Callable[[], T], *, close_result_on_timeout: bool = False
    ) -> T:
        if self._closed:
            raise ConnectionError("archive fetcher is closed")
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
            if close_result_on_timeout:
                old_executor.submit(_close_late_archive_client, future)
            self._executor = self._new_executor()
            old_executor.submit(
                _close_archive_clients, self._subtensor, self._substrate
            )
            self._subtensor = None
            self._substrate = None
            old_executor.shutdown(wait=False)
            with self._abandoned_workers_condition:
                if not future.done():
                    self._abandoned_workers += 1
                    future.add_done_callback(self._release_abandoned_worker)
            raise TimeoutError("archive request timed out") from error

    def _release_abandoned_worker[T](self, _future: Future[T]) -> None:
        with self._abandoned_workers_condition:
            self._abandoned_workers -= 1
            self._abandoned_workers_condition.notify_all()


def _close_late_archive_client[T](future: Future[T]) -> None:
    try:
        client = future.result()
    except Exception:  # noqa: BLE001 — cancelled or failed construction owns no client
        return
    _close_archive_clients(client)


def _close_archive_clients(*clients: object) -> None:
    for client in clients:
        close = getattr(client, "close", None)
        if close is not None:
            try:
                close()
            except Exception as error:  # noqa: BLE001 — still close the other client
                bt.logging.warning(
                    f"archive client cleanup failed: {type(error).__name__}"
                )


class LiveAlphaPriceProvider:
    """Archive-backed Alpha price/reserve provider for the R6 served runtime."""

    def __init__(  # noqa: PLR0913 — keyword-only validator-wiring seams
        self,
        *,
        config: LiveAlphaPriceProviderConfig,
        fetcher: AlphaSubnetInfoFetcher | None = None,
        sleep: Sleeper = sleep_decimal,
        now_fn: Callable[[], float] = time.monotonic,
        progress_fn: Callable[[], None] | None = None,
        deadline_exceeded_fn: Callable[[], bool] | None = None,
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
        self._progress_fn = progress_fn
        self._deadline_exceeded_fn = deadline_exceeded_fn
        self._snapshots: dict[tuple[int, int], AlphaPriceSnapshot] = {}
        self._series: OrderedDict[tuple[int, ResolutionWindow], AlphaPriceSeries] = (
            OrderedDict()
        )
        self._head_cache: tuple[float, int] | None = None
        self._first_timestamp_blocks: dict[int, int] = {}
        self._last_timestamp_blocks: dict[int, int] = {}
        self._probing = False

    @property
    def endpoint(self) -> str:
        return self._config.endpoint

    def validate_archive(self, *, netuid: int) -> None:
        """Fail closed unless mainnet has timestamp and positive pool history.

        ``netuid`` must be a known active mainnet Alpha subnet, not the chain
        netuid on which this validator happens to be registered. The probe
        resolves one 30-day boundary, exercising the same deep timestamp
        bisection as scoring, then reads just that historical pool.
        """
        previous_deadline = self._deadline_exceeded_fn
        deadline = self._now_fn() + LIVE_MARKET_DATA_ARCHIVE_PROBE_TIMEOUT_SECONDS
        self._deadline_exceeded_fn = lambda: (
            self._now_fn() >= deadline
            or (previous_deadline is not None and previous_deadline())
        )
        self._probing = True
        try:
            self._validate_archive(netuid=netuid)
        except Exception:  # noqa: BLE001 — fail closed without endpoint credentials
            raise AlphaMarketDataUnavailable(
                "mainnet archive readiness failed: mainnet identity, deep finalized "
                "timestamps and positive 30-day Alpha reserves are required"
            ) from None
        finally:
            self._deadline_exceeded_fn = previous_deadline
            self._probing = False

    def _validate_archive(self, *, netuid: int) -> None:
        if netuid <= 0:
            raise AlphaMarketDataUnavailable("archive probe requires an Alpha subnet")
        # A node that answers no genesis yet is retried like any missing
        # read; only a genesis that differs after normalization is refused.
        genesis = self._with_retry(
            lambda: require_archive_value(
                self._fetcher.genesis_hash(), "returned no genesis hash"
            )
        )
        if normalize_genesis_hash(genesis) != MAINNET_GENESIS_HASH:
            raise AlphaMarketDataUnavailable("archive is not Bittensor mainnet")
        finalized = self._with_retry(self._fetcher.finalized_block)
        if finalized <= 0:
            raise AlphaMarketDataUnavailable("archive has no finalized history")
        finalized_ms = self._timestamp_at_block(finalized)
        finalized_at = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
            milliseconds=finalized_ms
        )
        cutoff = finalized_at - LIVE_MARKET_DATA_ARCHIVE_LOOKBACK
        block = self.last_finalized_block_at_or_before(cutoff, now=finalized_at)
        historical_ms = self._timestamp_at_block(block)
        if (
            block >= finalized
            or historical_ms <= 0
            or historical_ms > _utc_timestamp_milliseconds(cutoff)
        ):
            raise AlphaMarketDataUnavailable("archive lacks 30-day timestamp history")
        info = self._with_retry(
            lambda: self._fetcher.subnet(netuid=netuid, block=block)
        )
        # Use the same reserve validation as scoring, never the SDK's spot price.
        alpha_snapshot_from_reserves(
            netuid=netuid,
            block=block,
            tao_rao=int(info.tao_in),
            alpha_rao=int(info.alpha_in),
        )

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

        block = first_block_at_or_after(
            timestamp_ms,
            finalized_block=self._with_retry(self._fetcher.finalized_block),
            timestamp_at=self._timestamp_at_block,
        )
        self._first_timestamp_blocks[timestamp_ms] = block
        return block

    def last_finalized_block_at_or_before(
        self, timestamp: datetime, *, now: datetime
    ) -> int:
        """Resolve the last finalized mainnet block at or before ``timestamp``."""
        _ = now  # Kept for validator call compatibility; chain state determines the block.
        timestamp_ms = _utc_timestamp_milliseconds(timestamp)
        cached = self._last_timestamp_blocks.get(timestamp_ms)
        if cached is not None:
            return cached

        block = last_block_at_or_before(
            timestamp_ms,
            finalized_block=self._with_retry(self._fetcher.finalized_block),
            timestamp_at=self._timestamp_at_block,
        )
        self._last_timestamp_blocks[timestamp_ms] = block
        return block

    def price_series(
        self, netuid: int, *, window: ResolutionWindow
    ) -> AlphaPriceSeries | None:
        key = (netuid, window)
        cached, current_block = self._current_block_for_series(key)
        if cached is not None:
            return cached

        sampling = SeriesSampling()
        for block in canonical_snapshot_blocks(window):
            if self._deadline_exceeded_fn is not None and self._deadline_exceeded_fn():
                # A 30d series is thousands of paced RPCs; the tick budget can
                # expire mid-series. Fetched snapshots stay in _snapshots, so
                # the resume tick re-enters warm. Deferral must not reach the
                # unavailable-target grace path — that would void a resolvable
                # coordinate — hence the dedicated exception.
                raise ResolutionDeadlineExceeded(
                    f"resolution budget exhausted mid-series netuid={netuid}"
                )
            if block > current_block:
                sampling.future_block()
            result = self._snapshot_at(
                netuid=netuid, block=block, current_block=current_block
            )
            snapshot = result.snapshot
            if snapshot is None:
                bt.logging.warning(
                    f"Alpha market-data snapshot skipped: netuid={netuid} block={block}"
                )
                if sampling.gap(connection_available=result.connection_available):
                    break
                continue
            sampling.sampled(snapshot)
        snapshots = sampling.finish(netuid=netuid, window=window)
        if not snapshots:
            return None
        series = AlphaPriceSeries(
            source=(
                f"{SUBTENSOR_RESERVE_PRICE_SOURCE}:netuid_{netuid}"
                f"_live_{snapshots[0].block}_{snapshots[-1].block}"
            ),
            netuid=netuid,
            snapshots=snapshots,
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
        except SNAPSHOT_FETCH_FAILURES as error:
            return _SnapshotFetchResult(
                snapshot=None,
                connection_available=not snapshot_failure_is_outage(error),
            )

    def _with_retry[T](self, operation: Callable[[], T]) -> T:
        # Attempt-level progress marks keep the watchdog honest: each attempt
        # is bounded (request timeout + capped backoff), while a wedged thread
        # stops marking and still trips it. The deadline check must live at
        # the same granularity: boundary bisections alone are ~2x24 lookups
        # with up to a ~68s retry ladder each, so a degraded archive could
        # otherwise hold one tick far past the watchdog window before
        # price_series ever runs.
        attempt = 0
        while True:
            attempt += 1
            if self._deadline_exceeded_fn is not None and self._deadline_exceeded_fn():
                raise ResolutionDeadlineExceeded(
                    "resolution budget exhausted during archive operation"
                )
            if self._progress_fn is not None:
                self._progress_fn()
            try:
                return operation()
            except ARCHIVE_FETCH_FAILURES as error:
                # The startup probe outlasts transient transport failures (a
                # 429 cooldown) until its deadline; missing history still fails
                # after max_attempts so a pruned node is refused promptly.
                probing = self._probing and not _is_missing_history(error)
                if not probing and attempt >= self._config.max_attempts:
                    raise retry_exhausted_failure(
                        error, f"archive request failed: {safe_error(error)}"
                    ) from None
                backoff = self._config.request_pause_seconds * (
                    Decimal(2) ** (attempt - 1)
                )
                if probing:
                    backoff = min(
                        backoff, LIVE_MARKET_DATA_ARCHIVE_PROBE_MAX_BACKOFF_SECONDS
                    )
                limit = "until probe deadline" if probing else self._config.max_attempts
                bt.logging.warning(
                    f"archive attempt {attempt}/{limit} failed; retrying: "
                    f"{safe_error(error)}"
                )
                self._sleep(backoff)

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


def validate_mainnet_archive(endpoint: str, *, netuid: int) -> None:
    """Read-only startup preflight; release its SDK clients before returning."""
    fetcher = BittensorSubnetInfoFetcher(endpoint)
    try:
        LiveAlphaPriceProvider(
            config=LiveAlphaPriceProviderConfig(endpoint=endpoint),
            fetcher=fetcher,
        ).validate_archive(netuid=netuid)
    finally:
        fetcher.close()


class GenesisClient(Protocol):
    def get_block_hash(self, block_id: int) -> str | None: ...

    def close(self) -> None: ...


def _substrate_genesis_client(endpoint: str, timeout_seconds: float) -> GenesisClient:
    # One RPC attempt per client: the SDK's own reconnect ladder (5 x 60 s by
    # default) would outlast the startup deadline.
    return SubstrateInterface(
        url=endpoint, max_retries=1, retry_timeout=timeout_seconds
    )


def read_chain_genesis(  # noqa: PLR0913 — keyword-only test-injection seams
    endpoint: str,
    *,
    deadline_seconds: float = CHAIN_GENESIS_READ_DEADLINE_SECONDS,
    attempt_timeout_seconds: float = CHAIN_GENESIS_ATTEMPT_TIMEOUT_SECONDS,
    client_factory: Callable[[str, float], GenesisClient] = _substrate_genesis_client,
    now_fn: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """Read-only startup chain identity; ``None`` once the deadline is spent.

    A 429, a DNS blip or a node still booting is retried with capped
    exponential backoff. Every attempt uses a fresh client, closed once it is
    built; a client whose constructor fails after connecting is released with
    its attempt, and its finalizer closes the socket. Each attempt runs on a
    daemon thread so a connect or RPC that never returns cannot hold startup
    past the deadline or keep the process alive.
    """
    label = safe_endpoint_label(endpoint)
    deadline = now_fn() + deadline_seconds
    backoff = CHAIN_GENESIS_INITIAL_BACKOFF_SECONDS
    attempt = 0
    reason = "deadline reached before the first attempt"
    while (remaining := deadline - now_fn()) > 0:
        attempt += 1
        timeout = min(attempt_timeout_seconds, remaining)
        try:
            genesis = _bounded_genesis_attempt(
                partial(client_factory, endpoint, timeout), timeout_seconds=timeout
            )
        except GenesisAttemptFailed as failed:
            reason = str(failed)
        except Exception as error:  # noqa: BLE001 — any failure is retried
            reason = f"{type(error).__name__}: {safe_error(error)}"
        else:
            if genesis:
                return genesis
            reason = "node returned no genesis hash"
        if now_fn() + backoff >= deadline:
            break
        bt.logging.warning(
            f"chain genesis read at {label} attempt {attempt} failed; "
            f"retrying in {backoff:.1f}s: {reason}"
        )
        sleep(backoff)
        backoff = min(backoff * 2, CHAIN_GENESIS_MAX_BACKOFF_SECONDS)
    bt.logging.error(
        f"chain genesis read at {label} failed after {attempt} attempts "
        f"within {deadline_seconds:.0f}s: {reason}"
    )
    return None


class GenesisAttemptFailed(Exception):
    """One genesis read attempt failed; carries only a redacted description."""


def _bounded_genesis_attempt(
    make_client: Callable[[], GenesisClient], *, timeout_seconds: float
) -> str | None:
    outcome: list[str | None] = []
    failure: list[str] = []
    finished = Event()

    def attempt() -> None:
        try:
            client = make_client()
            try:
                outcome.append(client.get_block_hash(0))
            finally:
                # A built client is closed here, even by an abandoned attempt
                # once it unblocks.
                _close_archive_clients(client)
        except Exception as error:  # noqa: BLE001 — reported to the caller
            # Keep only a description: the exception's traceback holds the
            # frames of a client whose constructor failed after connecting.
            # Holding it would leave that client (and its socket, closed by
            # its finalizer) alive until cyclic GC instead of releasing it
            # when this attempt ends.
            failure.append(f"{type(error).__name__}: {safe_error(error)}")
        finally:
            finished.set()

    # Daemon: an attempt wedged in DNS or the handshake must never keep the
    # process alive; the SDK client has no connect timeout covering DNS.
    Thread(target=attempt, name="chain-genesis-read", daemon=True).start()
    if not finished.wait(timeout_seconds):
        raise TimeoutError("chain genesis read timed out")
    if failure:
        raise GenesisAttemptFailed(failure[0])
    return outcome[0]


def _utc_timestamp_milliseconds(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AlphaMarketDataError("reveal_close must be timezone-aware")
    utc_value = value.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = utc_value - epoch
    return (
        elapsed.days * 86_400 + elapsed.seconds
    ) * 1_000 + elapsed.microseconds // 1_000
