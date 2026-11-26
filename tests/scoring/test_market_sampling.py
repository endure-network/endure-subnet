"""Boundary searches map a timestamp to the block a full bisection finds."""

from __future__ import annotations

import random

import pytest

from endure.scoring.market_data import AlphaMarketDataUnavailable
from endure.scoring.market_sampling import (
    first_block_at_or_after,
    last_block_at_or_before,
)

_BLOCK_MS = 12_000
_HEAD_MS = 1_790_000_000_000


def _chain(
    seed: int, blocks: int, *, missed_per_100k: int, repeated_per_100k: int
) -> list[int]:
    """Finalized timestamps: genesis stamps 0, then one block per 12 s slot."""
    rng = random.Random(seed)  # noqa: S311 — a seeded case generator, not secrecy
    now = _HEAD_MS - blocks * _BLOCK_MS
    timestamps = [0]
    for _ in range(1, blocks):
        roll = rng.randrange(100_000)
        if roll < repeated_per_100k and len(timestamps) > 1:
            step = 0
        elif roll < repeated_per_100k + missed_per_100k:
            step = _BLOCK_MS * rng.randint(2, 5)
        else:
            step = _BLOCK_MS
        now += step
        timestamps.append(now)
    return timestamps


def _bisected_boundary(timestamp_ms: int, timestamps: list[int], *, after: bool) -> int:
    """The first block reaching the timestamp, by bisection from block 0."""
    lower, upper = 0, len(timestamps) - 1
    while lower < upper:
        midpoint = lower + (upper - lower) // 2
        stamped = timestamps[midpoint]
        if stamped > timestamp_ms if after else stamped >= timestamp_ms:
            upper = midpoint
        else:
            lower = midpoint + 1
    return lower


def _targets(seed: int, timestamps: list[int]) -> list[int]:
    """Timestamps below the head: random, on a block, and either side of one."""
    rng = random.Random(seed)  # noqa: S311 — a seeded case generator, not secrecy
    head_ms = timestamps[-1]
    stamped = [timestamps[rng.randrange(1, len(timestamps))] for _ in range(40)]
    candidates = {
        *(rng.randrange(timestamps[1] - 3 * _BLOCK_MS, head_ms) for _ in range(80)),
        *stamped,
        *(value - 1 for value in stamped),
        *(value + 1 for value in stamped),
    }
    return sorted(value for value in candidates if value < head_ms)


_CHAINS = {
    "regular": _chain(1, 5_000, missed_per_100k=0, repeated_per_100k=0),
    "missed slots": _chain(2, 5_000, missed_per_100k=2_000, repeated_per_100k=0),
    "repeated stamps": _chain(
        3, 5_000, missed_per_100k=10_000, repeated_per_100k=5_000
    ),
    "a day per block": [
        0,
        *(_HEAD_MS - day * 86_400_000 for day in range(100, 0, -1)),
    ],
    "two blocks": [0, _HEAD_MS],
}


@pytest.mark.parametrize("name", list(_CHAINS))
def test_boundary_searches_find_the_blocks_a_full_bisection_finds(name: str) -> None:
    # Given: a finalized chain and timestamps across its history.
    timestamps = _CHAINS[name]
    head = len(timestamps) - 1

    for target in _targets(len(timestamps), timestamps):
        # When: both boundaries are resolved.
        first = first_block_at_or_after(
            target, finalized_block=head, timestamp_at=timestamps.__getitem__
        )
        # Then: each is the block a bisection from block 0 returns.
        assert first == _bisected_boundary(target, timestamps, after=False)
        last = _bisected_boundary(target, timestamps, after=True) - 1
        if last < 0:
            with pytest.raises(AlphaMarketDataUnavailable):
                last_block_at_or_before(
                    target, finalized_block=head, timestamp_at=timestamps.__getitem__
                )
        else:
            assert last == last_block_at_or_before(
                target, finalized_block=head, timestamp_at=timestamps.__getitem__
            )


def test_a_boundary_a_month_deep_takes_a_handful_of_reads() -> None:
    # Given: mainnet-like blocks, drifting about a hundred slots a month.
    timestamps = _chain(4, 226_000, missed_per_100k=20, repeated_per_100k=0)
    reads: list[int] = []

    def timestamp_at(block: int) -> int:
        reads.append(block)
        return timestamps[block]

    target = timestamps[10_000] + 5_000

    # When: the window start a month back is resolved.
    block = first_block_at_or_after(
        target, finalized_block=len(timestamps) - 1, timestamp_at=timestamp_at
    )

    # Then: it is the bisected block, read in a few lookups instead of ~18.
    assert block == _bisected_boundary(target, timestamps, after=False)
    assert len(reads) <= 8
