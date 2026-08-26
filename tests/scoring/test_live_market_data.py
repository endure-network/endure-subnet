from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count

import pytest
from async_substrate_interface.errors import MaxRetriesExceeded
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from endure.live.alpha_market_data import (
    LIVE_MARKET_DATA_MAX_ABANDONED_WORKERS,
    LIVE_MARKET_DATA_RATE_LIMIT_COOLDOWN_SECONDS,
    LIVE_MARKET_DATA_REQUEST_PAUSE_SECONDS,
    MAINNET_ARCHIVE_ENDPOINT,
    BittensorSubnetInfoFetcher,
    LiveAlphaPriceProvider,
    LiveAlphaPriceProviderConfig,
)
from endure.protocol.risk_miner import LatestPoolObservation, baseline_risk_bundle
from endure.scoring.market_data import (
    AlphaMarketDataError,
    AlphaMarketDataUnavailable,
    ResolutionWindow,
    alpha_snapshot_from_reserves,
)
from scripts.record_lending_market_fixtures import _row_from_reserves


@dataclass(frozen=True, slots=True)
class FakeDynamicInfo:
    tao_in: int
    alpha_in: int


@dataclass(slots=True)
class FakeSubnetFetcher:
    responses: dict[tuple[int, int | None], FakeDynamicInfo]
    current: int = 10_000
    finalized: int = 10_000
    timestamps_by_block: dict[int, int] = field(default_factory=dict)
    transient_failures: int = 0
    persistent_failure: bool = False
    failed_blocks: frozenset[int] = frozenset()
    failure_error: Exception | None = None
    current_failures: int = 0
    calls: list[tuple[int, int | None]] = field(default_factory=list)
    current_calls: int = 0

    def subnet(self, *, netuid: int, block: int | None = None) -> FakeDynamicInfo:
        self.calls.append((netuid, block))
        if self.persistent_failure:
            raise OSError("archive unavailable")
        if block in self.failed_blocks:
            raise self.failure_error or OSError("archive block unavailable")
        if self.transient_failures > 0:
            self.transient_failures -= 1
            raise TimeoutError("archive timeout")
        return self.responses[(netuid, block)]

    def current_block(self) -> int:
        self.current_calls += 1
        if self.current_failures > 0:
            self.current_failures -= 1
            raise TimeoutError("archive head unavailable")
        return self.current

    def finalized_block(self) -> int:
        return self.finalized

    def timestamp_at_block(self, block: int) -> int:
        return self.timestamps_by_block[block]


@dataclass(frozen=True, slots=True)
class FakeStorageValue:
    value: int


@dataclass(slots=True)
class FakeArchiveSubstrate:
    finalized: int
    timestamps_by_block: dict[int, int]
    query_hashes: list[str] = field(default_factory=list)

    def get_chain_finalised_head(self) -> str:
        return "finalized"

    def get_block_number(self, block_hash: str) -> int:
        assert block_hash == "finalized"
        return self.finalized

    def get_block_hash(self, block_id: int) -> str:
        return f"block-{block_id}"

    def query(
        self,
        module: str,
        storage_function: str,
        params: list[str] | None = None,
        block_hash: str | None = None,
    ) -> FakeStorageValue:
        assert module == "Timestamp"
        assert storage_function == "Now"
        assert params is None
        assert block_hash is not None
        self.query_hashes.append(block_hash)
        block = int(block_hash.removeprefix("block-"))
        return FakeStorageValue(value=self.timestamps_by_block[block])


@dataclass(slots=True)
class BlockingSubtensor:
    slow_netuids: frozenset[int] = frozenset()
    slow_current_block: bool = False
    sleep_seconds: float = 0.6
    calls: list[tuple[int, int | None]] = field(default_factory=list)

    def subnet(self, netuid: int, block: int | None = None) -> FakeDynamicInfo:
        self.calls.append((netuid, block))
        if netuid in self.slow_netuids:
            time.sleep(self.sleep_seconds)
        return FakeDynamicInfo(tao_in=5_000_000_000 + netuid, alpha_in=1_000_000_000)

    def get_current_block(self) -> int:
        if self.slow_current_block:
            time.sleep(self.sleep_seconds)
        return 9_999


@dataclass(slots=True)
class IndefinitelyBlockingSubtensor:
    release: threading.Event
    entered: threading.Event = field(default_factory=threading.Event)

    def subnet(self, netuid: int, block: int | None = None) -> FakeDynamicInfo:
        self.entered.set()
        self.release.wait()
        return FakeDynamicInfo(tao_in=5_000_000_000 + netuid, alpha_in=1_000_000_000)

    def get_current_block(self) -> int:
        self.entered.set()
        self.release.wait()
        return 9_999


@dataclass(slots=True)
class RateLimitedOperationSubtensor:
    calls: int = 0

    def subnet(self, netuid: int, block: int | None = None) -> FakeDynamicInfo:
        self.calls += 1
        raise InvalidStatus(Response(429, "Too Many Requests", Headers()))

    def get_current_block(self) -> int:
        self.calls += 1
        raise InvalidStatus(Response(429, "Too Many Requests", Headers()))


@dataclass(slots=True)
class ThreadRecordingSubtensor:
    constructed_thread_id: int = field(default_factory=threading.get_ident)
    used_thread_ids: set[int] = field(default_factory=set)

    def subnet(self, netuid: int, block: int | None = None) -> FakeDynamicInfo:
        self.used_thread_ids.add(threading.get_ident())
        return FakeDynamicInfo(tao_in=5_000_000_000 + netuid, alpha_in=1_000_000_000)

    def get_current_block(self) -> int:
        self.used_thread_ids.add(threading.get_ident())
        return 9_999


def _responses_for_blocks(
    netuid: int, blocks: tuple[int, ...]
) -> dict[tuple[int, int | None], FakeDynamicInfo]:
    return {
        (netuid, block): FakeDynamicInfo(
            tao_in=(1_000 + index) * 1_000_000_000,
            alpha_in=1_000_000_000,
        )
        for index, block in enumerate(blocks)
    }


def _reveal_close(seconds_after_epoch: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=seconds_after_epoch)


def _timestamp(seconds_after_epoch: int) -> int:
    return 1_767_225_600_000 + seconds_after_epoch * 1_000


def test_live_provider_uses_first_block_at_or_after_reveal_close() -> None:
    # Given: finalized timestamps straddle the reveal close, including a repeated timestamp.
    substrate = FakeArchiveSubstrate(
        finalized=6,
        timestamps_by_block={
            0: _timestamp(0),
            1: _timestamp(10),
            2: _timestamp(20),
            3: _timestamp(20),
            4: _timestamp(31),
            5: _timestamp(42),
            6: _timestamp(53),
        },
    )
    fetcher = BittensorSubnetInfoFetcher(endpoint="mock://archive")
    fetcher._make_substrate = lambda: substrate
    provider = LiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(request_pause_seconds=Decimal("0")),
        fetcher=fetcher,
    )

    # When: resolution maps a close exactly at the repeated timestamp.
    block = provider.block_for_reveal_close(
        _reveal_close(20), now=_reveal_close(10_000)
    )

    # Then: it selects the first matching block, not an estimate or later duplicate.
    assert block == 2


def test_live_provider_maps_misaligned_reveal_close_to_next_block() -> None:
    # Given: a reveal close falls between two finalized millisecond timestamps.
    fetcher = FakeSubnetFetcher(
        responses={},
        finalized=4,
        timestamps_by_block={
            0: _timestamp(0),
            1: _timestamp(9),
            2: _timestamp(21),
            3: _timestamp(33),
            4: _timestamp(45),
        },
    )
    provider = LiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(request_pause_seconds=Decimal("0")),
        fetcher=fetcher,
    )

    # When: resolution maps the misaligned close.
    block = provider.block_for_reveal_close(_reveal_close(10), now=_reveal_close(9_999))

    # Then: it selects the smallest block whose timestamp has reached the close.
    assert block == 2


def test_live_provider_reveal_close_mapping_ignores_cached_heads_and_tick_time() -> (
    None
):
    # Given: validators have different best-head caches and execute at different times.
    timestamps = {
        0: _timestamp(0),
        1: _timestamp(12),
        2: _timestamp(24),
        3: _timestamp(36),
        4: _timestamp(48),
    }
    first_fetcher = FakeSubnetFetcher(
        responses={}, current=5, finalized=4, timestamps_by_block=timestamps
    )
    second_fetcher = FakeSubnetFetcher(
        responses={}, current=50_000, finalized=4, timestamps_by_block=timestamps
    )
    first = LiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(request_pause_seconds=Decimal("0")),
        fetcher=first_fetcher,
        now_fn=lambda: 0.0,
    )
    second = LiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(request_pause_seconds=Decimal("0")),
        fetcher=second_fetcher,
        now_fn=lambda: 1_000_000.0,
    )
    first._head_cache = (0.0, 1)
    second._head_cache = (1_000_000.0, 49_000)

    # When: both validators resolve the same close.
    first_block = first.block_for_reveal_close(_reveal_close(25), now=_reveal_close(26))
    second_block = second.block_for_reveal_close(
        _reveal_close(25), now=_reveal_close(99_999)
    )

    # Then: finalized chain data alone determines the selected block.
    assert first_block == second_block == 3


def test_live_provider_defers_reveal_close_beyond_finalized_head() -> None:
    # Given: the finalized head has not yet reached the reveal close.
    fetcher = FakeSubnetFetcher(
        responses={},
        finalized=3,
        timestamps_by_block={
            0: _timestamp(0),
            1: _timestamp(12),
            2: _timestamp(24),
            3: _timestamp(36),
        },
    )
    provider = LiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(request_pause_seconds=Decimal("0")),
        fetcher=fetcher,
    )

    # When / Then: resolution defers rather than estimating an unfinalized block.
    with pytest.raises(AlphaMarketDataError, match="finalized"):
        provider.block_for_reveal_close(_reveal_close(37), now=_reveal_close(38))


def test_live_provider_derives_wall_clock_window_from_finalized_timestamps() -> None:
    # Given: non-uniform finalized block timestamps and validators with different heads.
    timestamps = {
        0: _timestamp(0),
        1: _timestamp(9),
        2: _timestamp(20),
        3: _timestamp(33),
        4: _timestamp(45),
    }
    first = LiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(request_pause_seconds=Decimal("0")),
        fetcher=FakeSubnetFetcher(
            responses={}, current=5, finalized=4, timestamps_by_block=timestamps
        ),
    )
    second = LiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(request_pause_seconds=Decimal("0")),
        fetcher=FakeSubnetFetcher(
            responses={}, current=50_000, finalized=4, timestamps_by_block=timestamps
        ),
    )

    # When: both map the (reveal_close, reveal_close + horizon] interval.
    close = _reveal_close(20)
    end = _reveal_close(35)
    first_window = ResolutionWindow(
        start_block=first.block_for_reveal_close(close, now=_reveal_close(40)),
        horizon_blocks=first.last_finalized_block_at_or_before(
            end, now=_reveal_close(40)
        )
        - first.block_for_reveal_close(close, now=_reveal_close(40)),
    )
    second_window = ResolutionWindow(
        start_block=second.block_for_reveal_close(close, now=_reveal_close(99_999)),
        horizon_blocks=second.last_finalized_block_at_or_before(
            end, now=_reveal_close(99_999)
        )
        - second.block_for_reveal_close(close, now=_reveal_close(99_999)),
    )

    # Then: the start excludes the close block and the end includes the last block <= end.
    assert (
        first_window
        == second_window
        == ResolutionWindow(start_block=2, horizon_blocks=1)
    )
    assert (
        first.last_finalized_block_at_or_before(
            _reveal_close(45), now=_reveal_close(40)
        )
        == 4
    )


def test_live_provider_defers_window_end_beyond_finalized_head() -> None:
    # Given: finalization covers the reveal close but not its horizon end.
    provider = LiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(request_pause_seconds=Decimal("0")),
        fetcher=FakeSubnetFetcher(
            responses={},
            finalized=3,
            timestamps_by_block={
                0: _timestamp(0),
                1: _timestamp(12),
                2: _timestamp(24),
                3: _timestamp(36),
            },
        ),
    )

    # When / Then: no end-block estimate is permitted before finalization.
    with pytest.raises(AlphaMarketDataUnavailable, match="finalized"):
        provider.last_finalized_block_at_or_before(
            _reveal_close(37), now=_reveal_close(38)
        )


def test_live_provider_uses_explicit_five_day_window_at_five_day_head() -> None:
    # Given: only the 5d archive blocks exist at the first production resolution time.
    blocks = tuple(range(1_600, 1_600 + 20 * 600, 600))
    fetcher = FakeSubnetFetcher(
        responses=_responses_for_blocks(44, blocks), current=blocks[-1]
    )
    provider = LiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(request_pause_seconds=Decimal("0")),
        fetcher=fetcher,
    )

    # When: the resolver requests only the due 5d horizon window.
    series = provider.price_series(
        44, window=ResolutionWindow(start_block=1_000, horizon_blocks=12_000)
    )

    # Then: no future 30d blocks are fetched, so the 5d signal remains live.
    assert series is not None
    assert tuple(snapshot.block for snapshot in series.snapshots) == blocks


def test_live_provider_assembles_series_at_canonical_cadence() -> None:
    # Given: three archive snapshots spaced by the R3 canonical 600-block cadence.
    fetcher = FakeSubnetFetcher(
        responses={
            (44, 1_600): FakeDynamicInfo(tao_in=2_000_000_000, alpha_in=1_000_000_000),
            (44, 2_200): FakeDynamicInfo(tao_in=3_000_000_000, alpha_in=1_000_000_000),
            (44, 2_800): FakeDynamicInfo(tao_in=4_000_000_000, alpha_in=1_000_000_000),
        }
    )
    provider = LiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(request_pause_seconds=Decimal("0")),
        fetcher=fetcher,
    )
    # When: the validator asks for the netuid's resolution series.
    series = provider.price_series(
        44, window=ResolutionWindow(start_block=1_000, horizon_blocks=1_800)
    )

    # Then: only canonical in-window blocks are fetched and serialized.
    assert series is not None
    assert tuple(snapshot.block for snapshot in series.snapshots) == (
        1_600,
        2_200,
        2_800,
    )
    assert fetcher.calls == [(44, 1_600), (44, 2_200), (44, 2_800)]
    assert series.source.endswith("netuid_44_live_1600_2800")


def test_live_provider_never_fetches_past_a_misaligned_window_end() -> None:
    # Given: the archive already has a cadence point after the requested window.
    fetcher = FakeSubnetFetcher(
        responses={
            (44, 602): FakeDynamicInfo(tao_in=2_000_000_000, alpha_in=1_000_000_000),
            (44, 1_202): FakeDynamicInfo(tao_in=3_000_000_000, alpha_in=1_000_000_000),
            (44, 1_802): FakeDynamicInfo(tao_in=4_000_000_000, alpha_in=1_000_000_000),
        },
        current=1_802,
    )
    provider = LiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(request_pause_seconds=Decimal("0")),
        fetcher=fetcher,
    )

    # When: the end block is not aligned to the 600-block sampling cadence.
    series = provider.price_series(
        44, window=ResolutionWindow(start_block=2, horizon_blocks=1_201)
    )

    # Then: the next cadence point is neither fetched nor included in the hash.
    assert series is not None
    assert tuple(snapshot.block for snapshot in series.snapshots) == (602, 1_202)
    assert fetcher.calls == [(44, 602), (44, 1_202)]


def test_live_provider_matches_recorder_decimal_derivation() -> None:
    # Given: the same reserve pair sent through the live helper and fixture recorder.
    snapshot = alpha_snapshot_from_reserves(
        netuid=8,
        block=7_402_800,
        tao_rao=1_000_000_000,
        alpha_rao=3_000_000_000,
    )

    # When: the recorder row is built with the shared helper.
    recorded = _row_from_reserves(
        netuid=8,
        block=7_402_800,
        tao_rao=1_000_000_000,
        alpha_rao=3_000_000_000,
    )

    # Then: both paths use the exact same half-even Decimal quantum.
    assert str(snapshot.price_tao_per_alpha) == recorded.price == "0.333333333"


def test_live_provider_retries_with_exponential_backoff() -> None:
    # Given: a transient archive timeout before a successful fetch.
    sleeps: list[Decimal] = []
    fetcher = FakeSubnetFetcher(
        responses={
            (44, 1_600): FakeDynamicInfo(tao_in=2, alpha_in=1),
            (44, 2_200): FakeDynamicInfo(tao_in=3, alpha_in=1),
        },
        transient_failures=1,
    )
    provider = LiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(),
        fetcher=fetcher,
        sleep=sleeps.append,
    )
    # When: the first archive call fails once.
    series = provider.price_series(
        44, window=ResolutionWindow(start_block=1_000, horizon_blocks=1_200)
    )

    # Then: the call is retried after the configured polite pause.
    assert series is not None
    assert sleeps == [LIVE_MARKET_DATA_REQUEST_PAUSE_SECONDS]
    assert fetcher.calls == [(44, 1_600), (44, 1_600), (44, 2_200)]


def test_live_provider_raises_transient_error_on_persistent_failure() -> None:
    # Given: an archive endpoint that never returns subnet state.
    fetcher = FakeSubnetFetcher(responses={}, persistent_failure=True)
    provider = LiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(
            max_attempts=2, request_pause_seconds=Decimal("0")
        ),
        fetcher=fetcher,
        sleep=lambda seconds: None,
    )
    # When / Then: an unavailable archive defers instead of fabricating a gap.
    with pytest.raises(AlphaMarketDataUnavailable):
        provider.price_series(
            44, window=ResolutionWindow(start_block=1_000, horizon_blocks=600)
        )


def test_live_provider_short_circuits_series_after_consecutive_archive_failures() -> (
    None
):
    # Given: one usable snapshot followed by an archive-wide connection outage.
    blocks = tuple(range(1_600, 1_600 + 5 * 600, 600))
    fetcher = FakeSubnetFetcher(
        responses=_responses_for_blocks(44, blocks[:1]),
        current=blocks[-1],
        failed_blocks=frozenset(blocks[1:]),
    )
    provider = LiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(
            max_attempts=2, request_pause_seconds=Decimal("0")
        ),
        fetcher=fetcher,
        sleep=lambda seconds: None,
    )
    window = ResolutionWindow(start_block=1_000, horizon_blocks=3_000)

    # When: consecutive archive failures follow a successful canonical block.
    with pytest.raises(AlphaMarketDataUnavailable):
        provider.price_series(44, window=window)

    # Then: the first two failed blocks exhaust their own retry budgets, and
    # the remaining historical blocks are unavailable for this tick.
    assert fetcher.calls == [
        (44, blocks[0]),
        (44, blocks[1]),
        (44, blocks[1]),
        (44, blocks[2]),
        (44, blocks[2]),
    ]
    assert not provider._series

    # And: a later tick retries the outage rather than reusing the partial series.
    with pytest.raises(AlphaMarketDataUnavailable):
        provider.price_series(44, window=window)
    assert fetcher.calls[-4:] == [
        (44, blocks[1]),
        (44, blocks[1]),
        (44, blocks[2]),
        (44, blocks[2]),
    ]


def test_live_provider_defers_one_unavailable_archive_block() -> None:
    # Given: a healthy 25-cadence window with one permanently missing archive block.
    blocks = tuple(range(1_600, 1_600 + 25 * 600, 600))
    missing = blocks[12]
    fetcher = FakeSubnetFetcher(
        responses=_responses_for_blocks(
            44, tuple(block for block in blocks if block != missing)
        ),
        current=blocks[-1],
        failed_blocks=frozenset((missing,)),
    )
    provider = LiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(
            max_attempts=2, request_pause_seconds=Decimal("0")
        ),
        fetcher=fetcher,
        sleep=lambda seconds: None,
    )

    # When / Then: a transient archive failure defers instead of creating a gap verdict.
    with pytest.raises(AlphaMarketDataUnavailable):
        provider.price_series(
            44, window=ResolutionWindow(start_block=1_000, horizon_blocks=15_000)
        )


def test_live_provider_gap_skips_sdk_retry_exhaustion_errors() -> None:
    # Given: one block whose fetch dies with the SDK's own MaxRetriesExceeded,
    # observed live 2026-07-07 when a transient DNS outage crashed resolution.
    blocks = tuple(range(1_600, 1_600 + 25 * 600, 600))
    missing = blocks[7]
    fetcher = FakeSubnetFetcher(
        responses=_responses_for_blocks(
            44, tuple(block for block in blocks if block != missing)
        ),
        current=blocks[-1],
        failed_blocks=frozenset((missing,)),
        failure_error=MaxRetriesExceeded(),
    )
    provider = LiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(
            max_attempts=2, request_pause_seconds=Decimal("0")
        ),
        fetcher=fetcher,
        sleep=lambda seconds: None,
    )

    # When: the resolver requests the window covering the poisoned block.
    # Then: the SDK error defers the horizon rather than creating a gap verdict.
    with pytest.raises(AlphaMarketDataUnavailable):
        provider.price_series(
            44, window=ResolutionWindow(start_block=1_000, horizon_blocks=15_000)
        )


def test_live_provider_caches_series_per_netuid_window() -> None:
    # Given: a one-block window already fetched once.
    fetcher = FakeSubnetFetcher(
        responses={
            (44, 1_600): FakeDynamicInfo(tao_in=2, alpha_in=1),
            (44, 2_200): FakeDynamicInfo(tao_in=3, alpha_in=1),
        }
    )
    provider = LiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(request_pause_seconds=Decimal("0")),
        fetcher=fetcher,
    )
    # When: two resolution attempts ask for the same block.
    window = ResolutionWindow(start_block=1_000, horizon_blocks=1_200)
    first = provider.price_series(44, window=window)
    second = provider.price_series(44, window=window)

    # Then: the second attempt reuses the immutable in-process series.
    assert first is not None
    assert second is not None
    assert first is second
    assert fetcher.current_calls == 1
    assert fetcher.calls == [(44, 1_600), (44, 2_200)]


def test_live_provider_does_not_memoize_future_truncated_series() -> None:
    # Given: the archive head initially covers only the first block in a window.
    ticks = iter((0.0, 31.0))
    fetcher = FakeSubnetFetcher(
        responses={
            (44, 1_600): FakeDynamicInfo(tao_in=2, alpha_in=1),
            (44, 2_200): FakeDynamicInfo(tao_in=3, alpha_in=1),
        },
        current=1_600,
    )
    provider = LiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(request_pause_seconds=Decimal("0")),
        fetcher=fetcher,
        now_fn=lambda: next(ticks),
    )
    window = ResolutionWindow(start_block=1_000, horizon_blocks=1_200)

    # When: the head advances and the same window is retried after the head TTL.
    with pytest.raises(AlphaMarketDataUnavailable):
        provider.price_series(44, window=window)
    fetcher.current = 2_200
    second = provider.price_series(44, window=window)

    # Then: the second call rebuilds after finalization and returns the full window.
    assert second is not None
    assert tuple(snapshot.block for snapshot in second.snapshots) == (1_600, 2_200)


def test_live_provider_bounds_series_and_snapshot_caches() -> None:
    # Given: many distinct windows for one process-lifetime provider.
    window_count = 370
    blocks = tuple(1_600 + index * 600 for index in range(window_count))
    fetcher = FakeSubnetFetcher(
        responses=_responses_for_blocks(44, blocks), current=blocks[-1]
    )
    provider = LiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(request_pause_seconds=Decimal("0")),
        fetcher=fetcher,
    )

    # When: each window is assembled successfully.
    for index in range(window_count):
        provider.price_series(
            44,
            window=ResolutionWindow(
                start_block=1_000 + index * 600,
                horizon_blocks=600,
            ),
        )

    # Then: both in-memory caches remain bounded deterministically.
    assert len(provider._series) <= 64
    assert len(provider._snapshots) <= 362


def test_live_provider_memoizes_head_probe_across_windows_within_ttl() -> None:
    ticks = count()
    blocks = tuple(range(1_600, 1_600 + 4 * 600, 600))
    fetcher = FakeSubnetFetcher(
        responses=_responses_for_blocks(44, blocks), current=blocks[-1]
    )
    provider = LiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(request_pause_seconds=Decimal("0")),
        fetcher=fetcher,
        now_fn=lambda: next(ticks),
    )

    provider.price_series(
        44, window=ResolutionWindow(start_block=1_000, horizon_blocks=600)
    )
    provider.price_series(
        44, window=ResolutionWindow(start_block=1_000, horizon_blocks=1_200)
    )

    assert fetcher.current_calls == 1


def test_live_provider_head_probe_failure_raises_without_series_memo() -> None:
    blocks = (1_600, 2_200)
    fetcher = FakeSubnetFetcher(
        responses=_responses_for_blocks(44, blocks),
        current=blocks[-1],
        current_failures=1,
    )
    provider = LiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(
            max_attempts=1, request_pause_seconds=Decimal("0")
        ),
        fetcher=fetcher,
    )
    window = ResolutionWindow(start_block=1_000, horizon_blocks=1_200)

    with pytest.raises(AlphaMarketDataUnavailable, match="archive head"):
        provider.price_series(44, window=window)
    assert not provider._series

    series = provider.price_series(44, window=window)

    assert series is not None
    assert tuple(snapshot.block for snapshot in series.snapshots) == blocks


def test_live_provider_defers_cached_series_when_head_regresses() -> None:
    ticks = iter((0.0, 31.0, 62.0))
    blocks = (1_600, 2_200)
    fetcher = FakeSubnetFetcher(
        responses=_responses_for_blocks(44, blocks), current=blocks[-1]
    )
    provider = LiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(request_pause_seconds=Decimal("0")),
        fetcher=fetcher,
        now_fn=lambda: next(ticks),
    )
    window = ResolutionWindow(start_block=1_000, horizon_blocks=1_200)

    first = provider.price_series(44, window=window)
    fetcher.current = blocks[0]
    with pytest.raises(AlphaMarketDataUnavailable):
        provider.price_series(44, window=window)
    fetcher.current = blocks[-1]
    second = provider.price_series(44, window=window)

    assert first is not None
    assert second is not None
    assert tuple(snapshot.block for snapshot in first.snapshots) == blocks
    assert tuple(snapshot.block for snapshot in second.snapshots) == blocks


def test_latest_pool_observation_uses_current_dynamic_info() -> None:
    # Given: a current-block DynamicInfo response for the miner baseline assembler.
    fetcher = FakeSubnetFetcher(
        responses={
            (8, None): FakeDynamicInfo(tao_in=5_000_000_000, alpha_in=2_000_000_000)
        },
        current=9_999,
    )
    provider = LiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(request_pause_seconds=Decimal("0")),
        fetcher=fetcher,
    )

    # When: the miner asks for the latest pool state.
    observation = provider.latest_pool_observation(8)

    # Then: price/depth are derived from current mainnet reserves.
    assert observation == LatestPoolObservation(
        price_rao=2_500_000_000,
        tao_reserve_rao=5_000_000_000,
    )
    assert fetcher.calls == [(8, None)]


def test_baseline_risk_bundle_skips_timed_out_netuids_without_wedging() -> None:
    # Given: a timed-out netuid's connection is abandoned, so recovery must use
    # a freshly built connection rather than reuse the wedged one.
    request_timeout = 0.2
    connections: list[BlockingSubtensor] = []

    def make_subtensor() -> BlockingSubtensor:
        connection = BlockingSubtensor(
            slow_netuids=frozenset((1,)) if not connections else frozenset(),
            sleep_seconds=0.6,
        )
        connections.append(connection)
        return connection

    fetcher = BittensorSubnetInfoFetcher(
        endpoint="mock://archive",
        request_timeout_seconds=request_timeout,
        subtensor_factory=make_subtensor,
    )
    provider = LiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(
            max_attempts=1,
            request_pause_seconds=Decimal("0"),
            request_timeout_seconds=request_timeout,
        ),
        fetcher=fetcher,
    )

    started = time.monotonic()
    bundle = baseline_risk_bundle(
        round_id="2026-07-07",
        netuids=(1, 2),
        latest_observation=provider.latest_pool_observation,
    )
    elapsed = time.monotonic() - started

    assert elapsed < request_timeout * 2.5
    assert tuple(asset.netuid for asset in bundle.assets) == (2,)


def test_latest_pool_observation_returns_none_when_current_block_times_out() -> None:
    request_timeout = 0.2
    fetcher = BittensorSubnetInfoFetcher(
        endpoint="mock://archive",
        request_timeout_seconds=request_timeout,
        subtensor=BlockingSubtensor(slow_current_block=True, sleep_seconds=0.6),
    )
    provider = LiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(
            max_attempts=1,
            request_pause_seconds=Decimal("0"),
            request_timeout_seconds=request_timeout,
        ),
        fetcher=fetcher,
    )

    assert provider.latest_pool_observation(2) is None


def test_fetcher_rebuilds_connection_after_subnet_timeout() -> None:
    request_timeout = 0.2
    connections: list[BlockingSubtensor] = []

    def make_subtensor() -> BlockingSubtensor:
        connection = BlockingSubtensor(
            slow_netuids=frozenset((9,)) if not connections else frozenset(),
            sleep_seconds=0.6,
        )
        connections.append(connection)
        return connection

    fetcher = BittensorSubnetInfoFetcher(
        endpoint="mock://archive",
        request_timeout_seconds=request_timeout,
        subtensor_factory=make_subtensor,
    )

    with pytest.raises(TimeoutError, match="archive request timed out"):
        fetcher.subnet(netuid=9)
    result = fetcher.subnet(netuid=9)

    assert int(result.tao_in) == 5_000_000_009
    assert len(connections) == 2
    assert connections[0] is not connections[1]
    assert connections[0].calls == [(9, None)]
    assert connections[1].calls == [(9, None)]


def test_fetcher_bounds_indefinitely_timed_out_archive_workers() -> None:
    # Given: every new archive connection blocks until the test releases it.
    request_timeout = 0.05
    release = threading.Event()
    connections: list[IndefinitelyBlockingSubtensor] = []

    def make_subtensor() -> IndefinitelyBlockingSubtensor:
        connection = IndefinitelyBlockingSubtensor(release=release)
        connections.append(connection)
        return connection

    fetcher = BittensorSubnetInfoFetcher(
        endpoint="mock://archive",
        request_timeout_seconds=request_timeout,
        subtensor_factory=make_subtensor,
    )

    try:
        # When: every active worker times out and cannot be cancelled by Python.
        for _ in range(LIVE_MARKET_DATA_MAX_ABANDONED_WORKERS):
            with pytest.raises(TimeoutError, match="archive request timed out"):
                fetcher.subnet(netuid=9)
        assert all(connection.entered.wait(timeout=1) for connection in connections)

        # Then: the cap rejects further work without creating another blocked worker.
        with pytest.raises(ConnectionError, match="timed-out workers at capacity"):
            fetcher.subnet(netuid=9)
        assert len(connections) == LIVE_MARKET_DATA_MAX_ABANDONED_WORKERS
        assert fetcher._abandoned_workers == LIVE_MARKET_DATA_MAX_ABANDONED_WORKERS
    finally:
        release.set()

    with fetcher._abandoned_workers_condition:
        assert fetcher._abandoned_workers_condition.wait_for(
            lambda: fetcher._abandoned_workers == 0, timeout=1
        )


def test_fetcher_connects_lazily_and_never_blocks_init_on_a_hanging_factory() -> None:
    # Given: a connect that hangs longer than the request timeout.
    request_timeout = 0.2
    factory_calls = 0

    def make_subtensor() -> BlockingSubtensor:
        nonlocal factory_calls
        factory_calls += 1
        time.sleep(0.6)
        return BlockingSubtensor()

    # When: the fetcher is constructed.
    started = time.monotonic()
    fetcher = BittensorSubnetInfoFetcher(
        endpoint="mock://archive",
        request_timeout_seconds=request_timeout,
        subtensor_factory=make_subtensor,
    )
    init_elapsed = time.monotonic() - started

    # Then: __init__ never touches the hanging factory on the main thread.
    assert factory_calls == 0
    assert init_elapsed < request_timeout

    # And: the hanging connect is wall-clock bounded on first use.
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="archive request timed out"):
        fetcher.subnet(netuid=1)
    assert factory_calls == 1
    assert time.monotonic() - started < request_timeout * 2.5


def test_fetcher_recovers_when_reconnect_raises_after_timeout() -> None:
    # Given: after a timeout, the reconnect itself fails once before healing.
    request_timeout = 0.2
    connections: list[BlockingSubtensor] = []
    attempts = count()

    def make_subtensor() -> BlockingSubtensor:
        index = next(attempts)
        if index == 1:
            raise ConnectionError("archive reconnect failed")
        connection = BlockingSubtensor(
            slow_netuids=frozenset((9,)) if index == 0 else frozenset(),
            sleep_seconds=0.6,
        )
        connections.append(connection)
        return connection

    fetcher = BittensorSubnetInfoFetcher(
        endpoint="mock://archive",
        request_timeout_seconds=request_timeout,
        subtensor_factory=make_subtensor,
    )

    # When: an operation times out, then the reconnect raises.
    with pytest.raises(TimeoutError, match="archive request timed out"):
        fetcher.subnet(netuid=9)
    with pytest.raises(ConnectionError):
        fetcher.subnet(netuid=9)

    # Then: the failed reconnect never wedges the executor (the #49 regression
    # raised RuntimeError 'cannot schedule new futures after shutdown'); the
    # next call reconnects cleanly.
    result = fetcher.subnet(netuid=9)
    assert int(result.tao_in) == 5_000_000_009
    assert len(connections) == 2


def test_fetcher_recovers_when_reconnect_hangs_after_timeout() -> None:
    # Given: after a timeout, the reconnect itself hangs once before healing.
    request_timeout = 0.2
    attempts = count()

    def make_subtensor() -> BlockingSubtensor:
        index = next(attempts)
        if index == 1:
            time.sleep(0.6)
        return BlockingSubtensor(
            slow_netuids=frozenset((9,)) if index == 0 else frozenset(),
            sleep_seconds=0.6,
        )

    fetcher = BittensorSubnetInfoFetcher(
        endpoint="mock://archive",
        request_timeout_seconds=request_timeout,
        subtensor_factory=make_subtensor,
    )

    # When: an operation times out, then the reconnect hangs.
    with pytest.raises(TimeoutError, match="archive request timed out"):
        fetcher.subnet(netuid=9)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="archive request timed out"):
        fetcher.subnet(netuid=9)

    # Then: the hung reconnect is bounded and a later good reconnect succeeds.
    assert time.monotonic() - started < request_timeout * 2.5
    result = fetcher.subnet(netuid=9)
    assert int(result.tao_in) == 5_000_000_009


def test_fetcher_constructs_and_uses_one_connection_on_a_single_worker_thread() -> None:
    # Given: a connection that records the thread it is built and used on.
    main_thread = threading.get_ident()
    built: list[ThreadRecordingSubtensor] = []

    def make_subtensor() -> ThreadRecordingSubtensor:
        connection = ThreadRecordingSubtensor()
        built.append(connection)
        return connection

    fetcher = BittensorSubnetInfoFetcher(
        endpoint="mock://archive",
        request_timeout_seconds=5.0,
        subtensor_factory=make_subtensor,
    )

    # When: several archive calls run without any timeout.
    fetcher.subnet(netuid=7)
    fetcher.subnet(netuid=8)
    block = fetcher.current_block()

    # Then: one connection is built off the main thread and every call runs on
    # the exact single-worker thread that constructed it.
    assert block == 9_999
    assert len(built) == 1
    connection = built[0]
    assert connection.constructed_thread_id != main_thread
    assert connection.used_thread_ids == {connection.constructed_thread_id}


def test_fetcher_backs_off_after_archive_rate_limit_429() -> None:
    # Given: the archive proxy rejects the WebSocket handshake with HTTP 429.
    clock = [0.0]
    connects: list[int] = []

    def make_subtensor() -> BlockingSubtensor:
        connects.append(1)
        raise InvalidStatus(Response(429, "Too Many Requests", Headers()))

    fetcher = BittensorSubnetInfoFetcher(
        endpoint="mock://archive",
        request_timeout_seconds=5.0,
        subtensor_factory=make_subtensor,
        now_fn=lambda: clock[0],
    )

    # When: the first fetch hits the 429, it arms a cooldown.
    with pytest.raises(ConnectionError):
        fetcher.subnet(netuid=9)
    assert len(connects) == 1

    # Then: within the cooldown window the archive is not hammered again.
    clock[0] = LIVE_MARKET_DATA_RATE_LIMIT_COOLDOWN_SECONDS - 1
    with pytest.raises(ConnectionError):
        fetcher.subnet(netuid=9)
    assert len(connects) == 1

    # And: once the cooldown expires a fresh probe is allowed through.
    clock[0] = LIVE_MARKET_DATA_RATE_LIMIT_COOLDOWN_SECONDS + 1
    with pytest.raises(ConnectionError):
        fetcher.subnet(netuid=9)
    assert len(connects) == 2


def test_fetcher_backs_off_after_operation_rate_limit_429() -> None:
    # Given: a cached archive connection that is rate-limited during subnet().
    clock = [0.0]
    connections: list[RateLimitedOperationSubtensor] = []

    def make_subtensor() -> RateLimitedOperationSubtensor:
        connection = RateLimitedOperationSubtensor()
        connections.append(connection)
        return connection

    fetcher = BittensorSubnetInfoFetcher(
        endpoint="mock://archive",
        request_timeout_seconds=5.0,
        subtensor_factory=make_subtensor,
        now_fn=lambda: clock[0],
    )

    # When: the cached connection receives a 429 after it has connected.
    with pytest.raises(ConnectionError):
        fetcher.subnet(netuid=9)
    assert connections[0].calls == 1

    # Then: the operation arms the same cooldown and drops the cached connection.
    clock[0] = LIVE_MARKET_DATA_RATE_LIMIT_COOLDOWN_SECONDS - 1
    with pytest.raises(ConnectionError):
        fetcher.subnet(netuid=9)
    assert len(connections) == 1

    # And: a new connection is attempted once the guarded cooldown expires.
    clock[0] = LIVE_MARKET_DATA_RATE_LIMIT_COOLDOWN_SECONDS + 1
    with pytest.raises(ConnectionError):
        fetcher.subnet(netuid=9)
    assert len(connections) == 2
    assert connections[1].calls == 1


def test_market_data_endpoint_default_is_mainnet_archive() -> None:
    # Given / When: no operator override is supplied.
    config = LiveAlphaPriceProviderConfig()

    # Then: Alpha market data defaults to mainnet archive, not --subtensor.*.
    assert config.endpoint == MAINNET_ARCHIVE_ENDPOINT


def test_live_provider_redacts_endpoint_credentials_after_retry_exhaustion() -> None:
    credential_url = "".join(
        ("wss://user:password", "@rpc.example.invalid/ws?token=secret")
    )
    provider = LiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(
            max_attempts=1, request_pause_seconds=Decimal("0")
        ),
        fetcher=FakeSubnetFetcher(responses={}),
    )

    def fail() -> int:
        raise RuntimeError(f"archive unavailable: {credential_url}")

    with pytest.raises(ConnectionError) as error_info:
        provider._with_retry(fail)

    rendered = str(error_info.value)
    assert "password" not in rendered
    assert "secret" not in rendered
    assert "archive unavailable: <redacted-endpoint>" in rendered

    def missing() -> int:
        raise LookupError("block missing")

    with pytest.raises(LookupError, match="block missing"):
        provider._with_retry(missing)


def test_live_provider_is_window_explicit_and_reentrant() -> None:
    # Given: one provider cache shared by interleaved 5d and 30d requests.
    blocks = tuple(range(1_600, 1_600 + 30 * 600, 600))
    fetcher = FakeSubnetFetcher(
        responses=_responses_for_blocks(44, blocks), current=blocks[-1]
    )
    provider = LiveAlphaPriceProvider(
        config=LiveAlphaPriceProviderConfig(request_pause_seconds=Decimal("0")),
        fetcher=fetcher,
    )

    # When: two different windows are requested back-to-back.
    short = provider.price_series(
        44, window=ResolutionWindow(start_block=1_000, horizon_blocks=3_000)
    )
    long = provider.price_series(
        44, window=ResolutionWindow(start_block=1_000, horizon_blocks=6_000)
    )

    # Then: no mutable provider window exists and each result matches its input window.
    assert not hasattr(provider, "set_resolution_window")
    assert short is not None
    assert long is not None
    assert tuple(snapshot.block for snapshot in short.snapshots) == blocks[:5]
    assert tuple(snapshot.block for snapshot in long.snapshots) == blocks[:10]
