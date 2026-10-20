"""Identical-forecast payout pooling (Sybil containment)."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final

import pytest

from endure.assessment.coordinates import AssessmentCoordinate, AssessmentEmaState
from endure.assessment.schemas.subnet_alpha_risk import (
    HORIZON_5D_SECONDS,
    RISK_SCHEMA_ID,
)
from endure.assessment.subnet_alpha_universe import StaticAlphaRiskUniverseProvider
from endure.protocol.canonical import canonical_bundle_bytes
from endure.protocol.risk_miner import LatestPoolObservation, baseline_risk_bundle
from endure.protocol.round_engine import DEFAULT_OFFSETS, compute_windows
from endure.scoring.assessment_orchestrator import (
    AssessmentScoringConfig,
    AssessmentScoringOrchestrator,
    FinalizedChainReads,
    ResolutionDeadlineExceeded,
    TieSeedReads,
    TieSeedUnavailable,
    chain_tie_seed,
)
from endure.scoring.duplicates import (
    POOL_WINDOW_ROUNDS,
    duplicate_pools,
    identical_forecast_groups,
    pool_sizes_of,
    tie_ordered,
)
from endure.scoring.market_data import (
    AlphaMarketDataUnavailable,
    FixtureAlphaPriceProvider,
)
from endure.scoring.market_sampling import utc_timestamp_milliseconds
from endure.scoring.risk.orchestrator import RiskScoringOrchestrator
from endure.scoring.weights import normalize_weights
from endure.storage.repository import (
    ROUND_STATE_CLOSED,
    ROUND_STATE_PARTIALLY_SCORED,
    ROUND_STATE_REVEALED,
    Storage,
)

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC).isoformat()
# NYSE sessions, oldest first: the test round windows use the session calendar.
SESSIONS = tuple(date(2026, 7, day) for day in (6, 7, 8, 9, 10, 13, 14))

type Values = Mapping[AssessmentCoordinate, int]


def _cell(netuid: int) -> AssessmentCoordinate:
    return AssessmentCoordinate.subnet_asset(
        netuid=netuid, horizon_seconds=HORIZON_5D_SECONDS, output="max_drawdown"
    )


FORECAST: Values = {_cell(1): 1200, _cell(4): 800}
NUDGED: Values = {_cell(1): 1201, _cell(4): 800}
OTHER: Values = {_cell(1): 900, _cell(4): 700}


def _pools(
    *rounds: Mapping[str, Values], scored: Sequence[str] | None = None
) -> tuple[frozenset[str], ...]:
    return duplicate_pools(
        (identical_forecast_groups(accepted) for accepted in rounds), scored
    )


def _sizes(
    *rounds: Mapping[str, Values], scored: Sequence[str] | None = None
) -> dict[str, int]:
    return pool_sizes_of(_pools(*rounds, scored=scored))


def _pooled_sizes(
    orchestrator: AssessmentScoringOrchestrator, scored: Sequence[str] | None = None
) -> dict[str, int]:
    return pool_sizes_of(orchestrator.duplicate_pools(scored))


def test_identical_values_in_one_round_form_one_pool() -> None:
    sizes = _sizes({"a": FORECAST, "b": dict(FORECAST), "c": FORECAST, "d": NUDGED})

    assert sizes == {"a": 3, "b": 3, "c": 3}


def test_one_moved_value_or_an_empty_submission_joins_no_pool() -> None:
    assert _sizes({"a": FORECAST, "b": NUDGED}) == {}
    assert _sizes({"a": {}, "b": {}}) == {}


def test_grouping_is_exact_so_a_copy_that_drops_a_target_does_not_pool() -> None:
    # Pooling is an exact-match containment: a partial copy forgoes its missing
    # targets' scores, which costs more than moving one value, and either way
    # it leaves the pool.
    partial = {_cell(1): FORECAST[_cell(1)]}

    assert _sizes({"a": FORECAST, "b": partial}) == {}


def test_a_pool_holds_members_that_later_leave_or_split() -> None:
    newest_first = [
        {"a": NUDGED, "b": FORECAST, "c": OTHER, "d": OTHER},
        {"a": FORECAST, "b": FORECAST, "c": FORECAST, "d": FORECAST},
    ]

    assert _sizes(*newest_first) == dict.fromkeys("abcd", 4)


def test_each_hotkey_pools_with_the_oldest_group_it_joined() -> None:
    newest_first = [
        {"a": FORECAST, "b": FORECAST, "x": FORECAST},
        {"a": OTHER, "b": OTHER, "c": OTHER},
    ]

    # Re-forming a group around a newcomer frees no one it left out.
    assert _sizes(*newest_first) == {"a": 3, "b": 3, "c": 3}


def test_a_copier_matching_several_hotkeys_joins_only_one_pool() -> None:
    # The copier joins one pool, its oldest, so the hotkeys it matched in
    # newer rounds keep their whole payout and its operator gains nothing.
    forecasts = [{_cell(1): 1000 + age, _cell(4): 800} for age in range(5)]
    newest_first = [
        {"copier": forecast, f"honest-{age}": forecast}
        for age, forecast in enumerate(forecasts)
    ]
    blends = dict.fromkeys(
        ("copier", "partner", *(f"honest-{age}" for age in range(5))), Decimal("0.9")
    )

    pools = _pools(*newest_first)
    alone = normalize_weights(blends)
    pooled = normalize_weights(blends, pools=pools)

    assert pools == (frozenset({"copier", "honest-4"}),)
    operator = ("copier", "partner")
    assert sum(pooled[key] for key in operator) < sum(alone[key] for key in operator)
    assert all(pooled[f"honest-{age}"] > alone[f"honest-{age}"] for age in range(4))


def test_a_predictable_miner_shares_its_oldest_pool_with_one_copy() -> None:
    # Validators cannot tell an original from its copy: a miner whose values
    # are reproduced exactly pools with the copy from its oldest round, and
    # the weight both give up is renormalized across every miner.
    forecasts = [{_cell(1): 1000 + age, _cell(4): 800} for age in range(3)]
    newest_first = [
        {"predictable": forecast, f"copy-{age}": forecast}
        for age, forecast in enumerate(forecasts)
    ]

    assert _sizes(*newest_first) == {"predictable": 2, "copy-2": 2}


def test_a_newly_scored_member_can_move_an_assignment() -> None:
    newest_first = [{"a": FORECAST, "c": FORECAST}, {"a": OTHER, "b": OTHER}]

    # Membership is live: once b is scored, a's oldest group counts.
    assert _sizes(*newest_first, scored=["a", "c"]) == {"a": 2, "c": 2}
    assert _sizes(*newest_first, scored=["a", "b", "c"]) == {"a": 2, "b": 2}


def test_a_pool_counts_only_scored_members() -> None:
    copies = {"a": FORECAST, "b": FORECAST, "c": FORECAST, "d": OTHER}

    assert _sizes(copies, scored=["a", "b", "d"]) == {"a": 2, "b": 2}
    assert _sizes(copies, scored=["a", "d"]) == {}


def test_pooled_copies_weigh_what_one_hotkey_would() -> None:
    blends = {key: Decimal("0.9") for key in ("copy-1", "copy-2", "copy-3", "solo")}
    pools = [{"copy-1", "copy-2", "copy-3"}]

    weights = normalize_weights(blends, pools=pools)

    assert weights["solo"] == Decimal("0.5")
    copies = weights["copy-1"] + weights["copy-2"] + weights["copy-3"]
    assert abs(copies - Decimal("0.5")) < Decimal("1E-26")
    assert normalize_weights(blends, pools=[]) == normalize_weights(blends)


def test_a_pool_keeps_its_best_member_and_each_member_gives_up_the_same() -> None:
    blends = {"best": Decimal(1), "weaker": Decimal("0.5"), "solo": Decimal(1)}

    weights = normalize_weights(blends, pools=[{"best", "weaker"}])

    # Sharpened 1 and 0.125 each give up 0.0625, so the pool keeps 1.
    assert weights == {
        "best": Decimal("0.46875"),
        "weaker": Decimal("0.03125"),
        "solo": Decimal("0.5"),
    }


def test_a_copy_weaker_than_the_cut_keeps_nothing_and_costs_nothing() -> None:
    pair = {"a": Decimal("0.9"), "b": Decimal("0.9")}
    solo = {"solo": Decimal("0.9")}

    weights = normalize_weights(
        {**pair, **solo, "weak": Decimal("0.1")}, pools=[{"a", "b", "weak"}]
    )

    # The cut stops at the weak copy: the pool still keeps one hotkey's weight.
    assert weights == {
        "a": Decimal("0.25"),
        "b": Decimal("0.25"),
        "solo": Decimal("0.5"),
        "weak": Decimal(0),
    }
    assert weights["a"] == normalize_weights({**pair, **solo}, pools=[{"a", "b"}])["a"]


@pytest.mark.parametrize("victim", ["0.3", "0.9"])
@pytest.mark.parametrize("copy", ["0.05", "0.3", "0.9"])
@pytest.mark.parametrize("partner", ["0", "0.5", "0.9"])
def test_matching_a_miner_never_pays_an_operator_with_at_most_half(
    victim: str, copy: str, partner: str
) -> None:
    # A copy gives up at least what it costs the rest of its pool, so the
    # weight they give up cannot raise an operator holding at most half.
    blends = {
        "victim": Decimal(victim),
        "copy": Decimal(copy),
        "partner": Decimal(partner),
        **{f"other-{n}": Decimal("0.9") for n in range(3)},
    }
    operator = ("copy", "partner")

    alone = normalize_weights(blends)
    pooled = normalize_weights(blends, pools=[{"victim", "copy"}])

    assert sum(alone[key] for key in operator) <= Decimal("0.5")
    assert sum(pooled[key] for key in operator) <= sum(alone[key] for key in operator)


def test_an_operator_with_most_of_the_pool_gains_from_a_copy() -> None:
    # The accepted limitation: the weight a copied miner gives up is
    # renormalized across every miner, the copier's other hotkeys included.
    blends = dict.fromkeys(("victim", "copy", "p-1", "p-2", "p-3"), Decimal("0.9"))
    operator = ("copy", "p-1", "p-2", "p-3")

    alone = normalize_weights(blends)
    pooled = normalize_weights(blends, pools=[{"victim", "copy"}])

    assert sum(alone[key] for key in operator) == Decimal("0.8")
    assert sum(pooled[key] for key in operator) == Decimal("0.875")


def _round_id(offset: int) -> str:
    return SESSIONS[offset].isoformat()


def _open(storage: Storage, offset: int, state: str | None) -> str:
    round_id = _round_id(offset)
    storage.open_round(
        windows=compute_windows(date.fromisoformat(round_id), offsets=DEFAULT_OFFSETS),
        schema_id=RISK_SCHEMA_ID,
        universe=StaticAlphaRiskUniverseProvider(netuids=(1, 4)).fetch_universe(
            round_id
        ),
        now_iso=NOW,
    )
    if state is not None:
        storage.set_round_state(round_id, RISK_SCHEMA_ID, state, now_iso=NOW)
    return round_id


def _orchestrator(  # noqa: PLR0913 — test seams
    storage: Storage,
    accepted: Mapping[str, Mapping[str, Values]],
    registered: Sequence[str] | None = None,
    reads: list[str] | None = None,
    *,
    blends: Mapping[str, Decimal] | None = None,
    tie_seed_reads: TieSeedReads | None = None,
) -> AssessmentScoringOrchestrator:
    scored = (
        dict.fromkeys(("a", "b", "c"), Decimal("0.9")) if blends is None else blends
    )
    for hotkey, blend in scored.items():
        storage.upsert_assessment_ema(
            RISK_SCHEMA_ID,
            AssessmentEmaState(hotkey, _cell(1), blend, 5),
            now_iso=NOW,
        )

    def accepted_values(round_id: str) -> dict[str, dict[AssessmentCoordinate, int]]:
        if reads is not None:
            reads.append(round_id)
        return {
            hotkey: dict(values)
            for hotkey, values in accepted.get(round_id, {}).items()
        }

    return AssessmentScoringOrchestrator(
        storage=storage,
        config=AssessmentScoringConfig(
            schema_id=RISK_SCHEMA_ID,
            horizons=(HORIZON_5D_SECONDS,),
            universe_members=lambda tickers: (),
            accepted_values=accepted_values,
            outputs=(),
            active_coordinates=frozenset(FORECAST),
        ),
        half_life_rounds=5,
        registered_hotkeys=None if registered is None else lambda: registered,
        tie_seed_reads=tie_seed_reads,
    )


@pytest.mark.parametrize(
    "state", [ROUND_STATE_REVEALED, ROUND_STATE_PARTIALLY_SCORED, ROUND_STATE_CLOSED]
)
def test_revealed_copies_share_one_payout(storage: Storage, state: str) -> None:
    revealed = _open(storage, 0, state)
    orchestrator = _orchestrator(
        storage, {revealed: {"a": FORECAST, "b": FORECAST, "c": OTHER}}
    )

    assert _pooled_sizes(orchestrator) == {"a": 2, "b": 2}
    weights = orchestrator.weights()
    assert weights["a"] == weights["b"]
    assert weights["c"] == Decimal("0.5")
    assert abs(weights["a"] + weights["b"] - Decimal("0.5")) < Decimal("1E-26")
    # Scores and blends are untouched: only the payout pools.
    assert set(orchestrator.blended_scores().values()) == {Decimal("0.9")}


def test_an_open_rounds_reveals_never_pool(storage: Storage) -> None:
    revealed = _open(storage, 0, ROUND_STATE_REVEALED)
    current = _open(storage, 1, None)
    orchestrator = _orchestrator(
        storage,
        {
            revealed: {"a": FORECAST, "b": NUDGED, "c": OTHER},
            current: {"a": FORECAST, "b": FORECAST, "c": FORECAST},
        },
    )

    assert _pooled_sizes(orchestrator) == {}
    assert len(set(orchestrator.weights().values())) == 1


def test_only_the_newest_revealed_rounds_pool(storage: Storage) -> None:
    rounds = [_open(storage, day, ROUND_STATE_CLOSED) for day in range(6)]
    distinct = {"a": FORECAST, "b": NUDGED, "c": OTHER}
    copies = {"a": FORECAST, "b": FORECAST, "c": OTHER}
    stale = {rounds[0]: copies, **{round_id: distinct for round_id in rounds[1:]}}
    edge = {rounds[1]: copies, **{round_id: distinct for round_id in rounds[2:]}}

    assert len(rounds) == POOL_WINDOW_ROUNDS + 1
    assert _pooled_sizes(_orchestrator(storage, stale)) == {}
    assert _pooled_sizes(_orchestrator(storage, edge)) == {"a": 2, "b": 2}


def test_each_revealed_round_is_read_once(storage: Storage) -> None:
    first = _open(storage, 0, ROUND_STATE_CLOSED)
    second = _open(storage, 1, None)
    copies = {"a": FORECAST, "b": FORECAST, "c": OTHER}
    reads: list[str] = []
    orchestrator = _orchestrator(storage, {first: copies, second: copies}, reads=reads)

    assert _pooled_sizes(orchestrator) == {"a": 2, "b": 2}
    assert _pooled_sizes(orchestrator, ["a", "c"]) == {}
    assert reads == [first]
    storage.set_round_state(second, RISK_SCHEMA_ID, ROUND_STATE_REVEALED, now_iso=NOW)
    assert _pooled_sizes(orchestrator) == {"a": 2, "b": 2}
    assert reads == [first, second]


def test_only_registered_copies_count_toward_a_pool(storage: Storage) -> None:
    revealed = _open(storage, 0, ROUND_STATE_CLOSED)
    copies = {"a": FORECAST, "b": FORECAST, "gone": FORECAST, "c": OTHER}
    registered = ["a", "b", "c"]
    orchestrator = _orchestrator(storage, {revealed: copies}, registered)

    assert _pooled_sizes(orchestrator) == {"a": 3, "b": 3, "gone": 3}
    assert _pooled_sizes(orchestrator, registered) == {"a": 2, "b": 2}
    weights = orchestrator.weights()
    assert set(weights) == {"a", "b", "c"}
    assert weights["c"] == Decimal("0.5")


def test_unscored_copies_never_cost_a_payout(storage: Storage) -> None:
    revealed = _open(storage, 0, ROUND_STATE_CLOSED)
    # "fresh" has no score yet and "zero" scored nothing: neither earns.
    copies = {"a": FORECAST, "fresh": FORECAST, "zero": FORECAST, "c": OTHER}
    orchestrator = _orchestrator(
        storage,
        {revealed: copies},
        ["a", "fresh", "zero", "c"],
        blends={"a": Decimal("0.9"), "zero": Decimal(0), "c": Decimal("0.9")},
    )

    weights = orchestrator.weights()
    assert orchestrator.pool_sizes() == {}
    assert weights["a"] == weights["c"] == Decimal("0.5")


def test_pools_are_those_behind_the_newest_weights(storage: Storage) -> None:
    revealed = _open(storage, 0, ROUND_STATE_CLOSED)
    copies = {"a": FORECAST, "b": FORECAST, "c": OTHER}
    registered = ["a", "b", "c"]
    orchestrator = _orchestrator(storage, {revealed: copies}, registered)

    assert orchestrator.pools() == ()
    assert orchestrator.pool_sizes() == {}
    orchestrator.weights()
    assert [set(pool) for pool in orchestrator.pools()] == [{"a", "b"}]
    assert orchestrator.pool_sizes() == {"a": 2, "b": 2}
    registered.remove("b")
    orchestrator.weights()
    assert orchestrator.pools() == ()
    assert orchestrator.pool_sizes() == {}


def test_without_a_chain_the_newest_revealed_round_seeds_the_tie_order(
    storage: Storage,
) -> None:
    first = _open(storage, 0, ROUND_STATE_CLOSED)
    second = _open(storage, 1, None)
    copies = {"a": FORECAST, "b": FORECAST, "c": FORECAST}
    orchestrator = _orchestrator(storage, {first: copies, second: copies})
    pool = (frozenset(copies),)

    assert orchestrator.duplicate_pools() == tie_ordered(pool, first)
    storage.set_round_state(second, RISK_SCHEMA_ID, ROUND_STATE_REVEALED, now_iso=NOW)
    assert orchestrator.duplicate_pools() == tie_ordered(pool, second)


# A finalized chain with a block every 12 s, stamped 5 s past each minute, so
# no block lands exactly a minute after a reveal close on the minute.
_GENESIS_MS: Final = 1_767_225_605_000
_BLOCK_MS: Final = 12_000


def _chain_hash(block: int) -> str:
    # Upper-case hex, as some clients answer; validators hash it lower-cased.
    return "0x" + hashlib.sha256(f"block-{block}".encode()).hexdigest().upper()


@dataclass(slots=True)
class _Chain:
    """Finalized blocks up to ``head``; with ``failure`` every read fails."""

    head: datetime
    failure: Exception | None = None
    lookups: int = 0

    def reads(self) -> FinalizedChainReads:
        self.lookups += 1

        def finalized_block() -> int:
            if self.failure is not None:
                raise self.failure
            return (utc_timestamp_milliseconds(self.head) - _GENESIS_MS) // _BLOCK_MS

        return FinalizedChainReads(
            finalized_block=finalized_block,
            timestamp_at_block=lambda block: _GENESIS_MS + block * _BLOCK_MS,
            block_hash=_chain_hash,
        )


def _reveal_close(storage: Storage, round_id: str) -> datetime:
    windows = storage.round_windows(round_id, RISK_SCHEMA_ID)
    assert windows is not None
    return windows.reveal_close


def _seed(reveal_close: datetime) -> str:
    """The hash of the first block stamped a minute or more after reveal close."""
    target_ms = utc_timestamp_milliseconds(reveal_close + timedelta(minutes=1))
    return _chain_hash(-(-(target_ms - _GENESIS_MS) // _BLOCK_MS)).lower()


def test_a_seed_is_the_first_block_stamped_a_minute_or_more_after_reveal_close() -> (
    None
):
    reveal_close = datetime(2026, 9, 20, tzinfo=UTC)
    chain = _Chain(head=reveal_close + timedelta(minutes=1))

    # The head is the block stamped 00:00:53; 00:01:05 is the seed's block.
    assert chain_tie_seed(chain.reads(), reveal_close) is None
    chain.head += timedelta(seconds=5)
    assert chain_tie_seed(chain.reads(), reveal_close) == _seed(reveal_close)
    # A block stamped exactly a minute after reveal close is the seed's block.
    on_a_block = datetime(2026, 9, 19, 23, 59, 5, tzinfo=UTC)
    target_ms = utc_timestamp_milliseconds(on_a_block) + 60_000
    assert (target_ms - _GENESIS_MS) % _BLOCK_MS == 0
    assert chain_tie_seed(chain.reads(), on_a_block) == _seed(on_a_block)
    # A finalized head stamped exactly then is that block.
    chain.head = on_a_block + timedelta(minutes=1)
    assert chain_tie_seed(chain.reads(), on_a_block) == _seed(on_a_block)


def test_live_ties_are_drawn_from_the_first_block_a_minute_after_reveal_close(
    storage: Storage,
) -> None:
    first = _open(storage, 0, ROUND_STATE_CLOSED)
    second = _open(storage, 1, ROUND_STATE_REVEALED)
    copies = {"a": FORECAST, "b": FORECAST, "c": FORECAST}
    chain = _Chain(head=_reveal_close(storage, second) + timedelta(hours=1))
    orchestrator = _orchestrator(
        storage, {first: copies, second: copies}, tie_seed_reads=chain.reads
    )
    pools = orchestrator.duplicate_pools()

    assert pools == tie_ordered(
        (frozenset(copies),), _seed(_reveal_close(storage, second))
    )
    # Later votes reuse the seed; a restarted validator reads the same block.
    assert orchestrator.duplicate_pools() == pools
    assert chain.lookups == 1
    restarted = _orchestrator(
        storage, {first: copies, second: copies}, tie_seed_reads=chain.reads
    )
    assert restarted.duplicate_pools() == pools
    assert chain.lookups == 2  # noqa: PLR2004 — one read per process


def test_until_the_newest_seed_is_known_the_window_ends_one_round_earlier(
    storage: Storage,
) -> None:
    rounds = [_open(storage, day, ROUND_STATE_CLOSED) for day in range(6)]
    distinct = {"a": FORECAST, "b": NUDGED, "c": OTHER}
    copies = {"a": FORECAST, "b": FORECAST, "c": OTHER}
    # The finalized head has passed round 4's seed block, not yet round 5's.
    chain = _Chain(head=_reveal_close(storage, rounds[5]) + timedelta(seconds=30))
    orchestrator = _orchestrator(
        storage,
        {rounds[0]: copies, **{round_id: distinct for round_id in rounds[1:]}},
        tie_seed_reads=chain.reads,
    )
    pool = (frozenset({"a", "b"}),)

    assert orchestrator.duplicate_pools() == tie_ordered(
        pool, _seed(_reveal_close(storage, rounds[4]))
    )
    chain.head += timedelta(minutes=1)
    assert orchestrator.duplicate_pools() == ()


def test_a_seed_the_archive_cannot_read_keeps_the_earlier_window(
    storage: Storage,
) -> None:
    first = _open(storage, 0, ROUND_STATE_CLOSED)
    second = _open(storage, 1, ROUND_STATE_REVEALED)
    copies = {"a": FORECAST, "b": FORECAST, "c": FORECAST}
    pool = (frozenset(copies),)
    earlier = tie_ordered(pool, _seed(_reveal_close(storage, first)))
    newest = tie_ordered(pool, _seed(_reveal_close(storage, second)))
    assert earlier != newest
    chain = _Chain(head=_reveal_close(storage, second))
    orchestrator = _orchestrator(
        storage, {first: copies, second: copies}, tie_seed_reads=chain.reads
    )
    assert orchestrator.duplicate_pools() == earlier

    chain.head += timedelta(hours=1)
    for failure in (
        ConnectionError("archive down"),
        LookupError("missing block hash"),
        AlphaMarketDataUnavailable("timestamp not covered"),
        ResolutionDeadlineExceeded("seed deadline spent"),
    ):
        chain.failure = failure
        assert orchestrator.duplicate_pools() == earlier
    # Anything else is a bug, not an archive that cannot answer.
    chain.failure = TypeError("bug")
    with pytest.raises(TypeError):
        orchestrator.duplicate_pools()
    chain.failure = None
    assert orchestrator.duplicate_pools() == newest


def test_while_the_archive_shows_neither_newest_seed_votes_keep_the_last_window(
    storage: Storage,
) -> None:
    copies = dict.fromkeys("abcde", FORECAST)
    # Rounds revealed during the outage add a second group of copies.
    later = {**copies, "x": OTHER, "y": OTHER}
    first, second = frozenset(copies), frozenset({"x", "y"})
    rounds = [_open(storage, 0, ROUND_STATE_CLOSED)]
    accepted: dict[str, Mapping[str, Values]] = {rounds[0]: copies}
    chain = _Chain(head=datetime(2027, 1, 1, tzinfo=UTC))
    blends = dict.fromkeys(later, Decimal("0.9"))
    orchestrator = _orchestrator(
        storage, accepted, blends=blends, tie_seed_reads=chain.reads
    )

    def seeded_by(round_id: str, *pools: frozenset[str]) -> tuple[tuple[str, ...], ...]:
        return tie_ordered(pools, _seed(_reveal_close(storage, round_id)))

    def reveal(day: int, values: Mapping[str, Values]) -> None:
        rounds.append(_open(storage, day, ROUND_STATE_REVEALED))
        accepted[rounds[day]] = values

    for day in (1, 2):
        reveal(day, copies)
        assert orchestrator.duplicate_pools() == seeded_by(rounds[day], first)
    # The archive fails while two more rounds reveal.
    chain.failure = ConnectionError("archive down")
    reveal(3, later)
    reveal(4, later)
    orders = {seeded_by(round_id, first) for round_id in rounds[1:]}
    assert len(orders) == len(rounds[1:])
    # Votes keep the last window's rounds with its seed, so copies revealed
    # once that seed was public join no pool.
    assert orchestrator.duplicate_pools() == seeded_by(rounds[2], first)
    # A process that has seeded no window cannot vote.
    restarted = _orchestrator(
        storage, accepted, blends=blends, tie_seed_reads=chain.reads
    )
    with pytest.raises(TieSeedUnavailable):
        restarted.duplicate_pools()
    chain.failure = None
    newest = seeded_by(rounds[4], first, second)
    assert orchestrator.duplicate_pools() == newest
    assert restarted.duplicate_pools() == newest


def test_votes_keep_a_window_seeded_from_the_round_before_the_newest(
    storage: Storage,
) -> None:
    copies = dict.fromkeys("abcde", FORECAST)
    pool = (frozenset(copies),)
    rounds = [_open(storage, 0, ROUND_STATE_CLOSED)]
    accepted: dict[str, Mapping[str, Values]] = {rounds[0]: copies}
    chain = _Chain(head=_reveal_close(storage, rounds[0]) + timedelta(hours=1))
    orchestrator = _orchestrator(
        storage,
        accepted,
        blends=dict.fromkeys(copies, Decimal("0.9")),
        tie_seed_reads=chain.reads,
    )
    seeded_by_first = tie_ordered(pool, _seed(_reveal_close(storage, rounds[0])))
    assert orchestrator.duplicate_pools() == seeded_by_first

    for day in (1, 2):
        rounds.append(_open(storage, day, ROUND_STATE_REVEALED))
        accepted[rounds[day]] = copies
    # Round 2's seed block is not final yet, so the window ends at round 1.
    chain.head = _reveal_close(storage, rounds[2]) + timedelta(seconds=30)
    seeded_by_round_1 = tie_ordered(pool, _seed(_reveal_close(storage, rounds[1])))
    assert seeded_by_round_1 != seeded_by_first
    assert orchestrator.duplicate_pools() == seeded_by_round_1
    # The archive then fails while round 3 reveals: votes keep that window.
    chain.failure = ConnectionError("archive down")
    rounds.append(_open(storage, 3, ROUND_STATE_REVEALED))
    accepted[rounds[3]] = copies
    assert orchestrator.duplicate_pools() == seeded_by_round_1


def test_a_vote_with_no_known_seed_fails_instead_of_guessing(
    storage: Storage,
) -> None:
    first = _open(storage, 0, ROUND_STATE_CLOSED)
    copies = {"a": FORECAST, "b": FORECAST, "c": OTHER}
    chain = _Chain(head=_reveal_close(storage, first))

    orchestrator = _orchestrator(storage, {first: copies}, tie_seed_reads=chain.reads)
    assert orchestrator.duplicate_pools() == ()
    second = _open(storage, 1, ROUND_STATE_REVEALED)
    orchestrator = _orchestrator(
        storage, {first: copies, second: copies}, tie_seed_reads=chain.reads
    )
    with pytest.raises(TieSeedUnavailable):
        orchestrator.duplicate_pools()


def test_the_tie_order_depends_only_on_the_seed_and_the_hotkeys() -> None:
    hotkeys = [f"hotkey-{index}" for index in range(5)]

    assert tie_ordered([hotkeys], "2026-10-13") == tie_ordered(
        [frozenset(reversed(hotkeys))], "2026-10-13"
    )
    assert sorted(tie_ordered([hotkeys], "2026-10-13")[0]) == hotkeys


def test_every_validator_hashes_the_same_domain_separated_tie_order() -> None:
    # The order is part of the vote, so its hash is pinned: sha256 of
    # "endure.pool_tie_order.v1:<seed>:<hotkey>".
    assert tie_ordered([("a", "b", "c", "d")], "0xseed") == (("d", "b", "a", "c"),)


def test_over_unknown_seeds_each_member_leads_the_tie_order_equally_often() -> None:
    hotkeys = ("a", "b", "c")
    firsts = Counter(
        tie_ordered([hotkeys], f"round-{index}")[0][0] for index in range(600)
    )

    assert set(firsts) == set(hotkeys)
    assert all(150 <= count <= 250 for count in firsts.values())


def test_a_hotkey_ground_against_known_seeds_leads_no_more_chain_drawn_orders() -> None:
    # Round IDs are scheduled, so an operator can grind a hotkey that leads a
    # victim under every one of them; a block hash after reveal close is not.
    round_ids = [f"2026-10-{day}" for day in range(15, 20)]
    ground = next(
        candidate
        for candidate in (f"attacker-{index}" for index in range(10_000))
        if all(
            tie_ordered([("victim", candidate)], round_id)[0][0] == candidate
            for round_id in round_ids
        )
    )
    blocks = [
        "0x" + hashlib.sha256(f"block-{index}".encode()).hexdigest()
        for index in range(400)
    ]
    leads = sum(
        tie_ordered([("victim", ground)], block)[0][0] == ground for block in blocks
    )

    assert 150 <= leads <= 250


def _latest(netuid: int) -> LatestPoolObservation:
    del netuid
    return LatestPoolObservation(price_rao=963_900_000, tao_reserve_rao=1_000_036_100)


def test_risk_pools_stored_bundles_by_their_values_alone(storage: Storage) -> None:
    round_id = _open(storage, 0, None)
    bundle = baseline_risk_bundle(
        round_id=round_id, netuids=(1, 4), latest_observation=_latest
    ).to_canonical_payload()
    relabeled = json.loads(json.dumps(bundle))
    for output in relabeled["assets"][0]["outputs"]:
        output["confidence_bps"] = 9000
        output["reason_codes"] = ["fleet_model"]
    moved = json.loads(json.dumps(bundle))
    moved["assets"][1]["outputs"][1]["value"] += 1
    for hotkey, payload in (("a", bundle), ("b", relabeled), ("c", moved)):
        storage.record_commit(round_id, RISK_SCHEMA_ID, hotkey, "ab" * 32, now_iso=NOW)
        storage.record_reveal(
            round_id,
            RISK_SCHEMA_ID,
            hotkey,
            bundle_json=canonical_bundle_bytes(payload).decode("utf-8"),
            nonce_hex="cd" * 16,
            accepted=True,
            rejection_code=None,
            now_iso=NOW,
        )
    orchestrator = RiskScoringOrchestrator(
        storage=storage,
        price_provider=FixtureAlphaPriceProvider({}),
        half_life_rounds=5,
        reveal_close_block=lambda _: 1_000,
    )

    assert _pooled_sizes(orchestrator) == {}
    storage.set_round_state(round_id, RISK_SCHEMA_ID, ROUND_STATE_REVEALED, now_iso=NOW)
    assert _pooled_sizes(orchestrator) == {"a": 2, "b": 2}


def test_revealed_round_ids_lists_post_reveal_rounds_newest_first(
    storage: Storage,
) -> None:
    states = [ROUND_STATE_CLOSED, ROUND_STATE_PARTIALLY_SCORED, ROUND_STATE_REVEALED]
    rounds = [_open(storage, day, state) for day, state in enumerate(states)]
    _open(storage, len(states), None)

    assert storage.revealed_round_ids(RISK_SCHEMA_ID, limit=5) == rounds[::-1]
    assert storage.revealed_round_ids(RISK_SCHEMA_ID, limit=2) == rounds[:0:-1]
    assert storage.revealed_round_ids("other.schema", limit=5) == []
