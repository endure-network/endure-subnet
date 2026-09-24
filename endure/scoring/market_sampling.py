"""Alpha market-data sampling decisions (risk scope spec §Market-data extension).

Which blocks are sampled, how a timestamp maps to a finalized block, and when
gaps void a series determine realized values and therefore scores, so they
live in the watched tree. Archive I/O stays in ``endure.live``: the boundary
searches receive the timestamp lookup as a callable and issue exactly the same
lookups in the same order.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from endure.scoring.market_data import (
    AlphaMarketDataUnavailable,
    AlphaPriceSnapshot,
    ResolutionWindow,
)
from endure.scoring.risk.observables import CANONICAL_ALPHA_SNAPSHOT_CADENCE_BLOCKS

# Consecutive archive-unavailable snapshots that abandon a series early.
MAX_CONSECUTIVE_ARCHIVE_GAPS: Final = 2


def canonical_snapshot_blocks(window: ResolutionWindow) -> tuple[int, ...]:
    """Sample every cadence step after the window start through its end."""
    first_block = window.start_block + CANONICAL_ALPHA_SNAPSHOT_CADENCE_BLOCKS
    return tuple(
        range(
            first_block,
            window.end_block + 1,
            CANONICAL_ALPHA_SNAPSHOT_CADENCE_BLOCKS,
        )
    )


def first_block_at_or_after(
    timestamp_ms: int, *, finalized_block: int, timestamp_at: Callable[[int], int]
) -> int:
    """The first finalized block whose timestamp is at or after ``timestamp_ms``."""
    finalized_timestamp = timestamp_at(finalized_block)
    if finalized_timestamp < timestamp_ms:
        raise AlphaMarketDataUnavailable(
            "timestamp is not yet covered by the finalized archive head"
        )
    if finalized_timestamp == timestamp_ms:
        return finalized_block
    lower_block = 0
    upper_block = finalized_block
    while lower_block < upper_block:
        midpoint = lower_block + (upper_block - lower_block) // 2
        if timestamp_at(midpoint) >= timestamp_ms:
            upper_block = midpoint
        else:
            lower_block = midpoint + 1
    return lower_block


def last_block_at_or_before(
    timestamp_ms: int, *, finalized_block: int, timestamp_at: Callable[[int], int]
) -> int:
    """The last finalized block whose timestamp is at or before ``timestamp_ms``."""
    finalized_timestamp = timestamp_at(finalized_block)
    if finalized_timestamp < timestamp_ms:
        raise AlphaMarketDataUnavailable(
            "timestamp is not yet covered by the finalized archive head"
        )
    if finalized_timestamp == timestamp_ms:
        return finalized_block
    lower_block = 0
    upper_block = finalized_block
    while lower_block < upper_block:
        midpoint = lower_block + (upper_block - lower_block) // 2
        if timestamp_at(midpoint) > timestamp_ms:
            upper_block = midpoint
        else:
            lower_block = midpoint + 1
    last_block = lower_block - 1
    if last_block < 0:
        raise AlphaMarketDataUnavailable(
            "timestamp precedes the finalized archive history"
        )
    return last_block


class SeriesSampling:
    """Gap policy for one series: which missing snapshots void or end it."""

    def __init__(self) -> None:
        self.snapshots: list[AlphaPriceSnapshot] = []
        self._skipped_future_block = False
        self._archive_unavailable = False
        self._consecutive_archive_gaps = 0

    def future_block(self) -> None:
        """A canonical block past the finalized head; the series is not ready."""
        self._skipped_future_block = True

    def gap(self, *, connection_available: bool) -> bool:
        """Record a missing snapshot; ``True`` means stop sampling this series."""
        if connection_available:
            # A definitive gap (missing pool) is skipped, not an outage.
            self._consecutive_archive_gaps = 0
            return False
        self._archive_unavailable = True
        self._consecutive_archive_gaps += 1
        return self._consecutive_archive_gaps >= MAX_CONSECUTIVE_ARCHIVE_GAPS

    def sampled(self, snapshot: AlphaPriceSnapshot) -> None:
        self._consecutive_archive_gaps = 0
        self.snapshots.append(snapshot)

    def finish(
        self, *, netuid: int, window: ResolutionWindow
    ) -> tuple[AlphaPriceSnapshot, ...]:
        """Void the series on any outage or unfinalized block, else return it."""
        if self._archive_unavailable:
            raise AlphaMarketDataUnavailable(
                f"archive data unavailable for netuid={netuid} window={window}"
            )
        if self._skipped_future_block:
            raise AlphaMarketDataUnavailable(
                f"archive head has not finalized netuid={netuid} window={window}"
            )
        return tuple(self.snapshots)
