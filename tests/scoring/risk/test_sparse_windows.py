from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext

import pytest

from endure.assessment.coordinates import AssessmentCoordinate
from endure.assessment.schemas.subnet_alpha_risk import (
    HORIZON_5D_SECONDS,
    RISK_SCHEMA_ID,
    RiskOutput,
)
from endure.assessment.subnet_alpha_universe import StaticAlphaRiskUniverseProvider
from endure.protocol.round_engine import DEFAULT_OFFSETS, compute_windows
from endure.scoring.context import TR_CONTEXT
from endure.scoring.market_data import (
    AlphaMarketDataError,
    AlphaPriceSeries,
    AlphaPriceSnapshot,
    FixtureAlphaPriceProvider,
)
from endure.scoring.risk.observables import (
    CANONICAL_ALPHA_SNAPSHOT_CADENCE_BLOCKS,
    MIN_REALIZED_WINDOW_SNAPSHOTS,
    filter_snapshots_for_window,
    horizon_seconds_to_blocks,
    liquidity_depth_rao,
    max_drawdown_bps,
    realized_volatility_bps,
    should_void_realized_window,
    twap_price_rao,
)
from endure.scoring.risk.orchestrator import RiskScoringOrchestrator
from endure.storage.repository import Storage

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=UTC).isoformat()
ROUND = "2026-07-08"
WINDOW_START_BLOCK = 10_000
NETUID = 44
CADENCE = CANONICAL_ALPHA_SNAPSHOT_CADENCE_BLOCKS
HORIZON_BLOCKS = horizon_seconds_to_blocks(HORIZON_5D_SECONDS)


@dataclass(frozen=True, slots=True)
class SparsePattern:
    name: str
    offsets: tuple[int, ...]
    resolver_volatility_status: str


def _snapshot(offset: int, price: Decimal, reserve: int) -> AlphaPriceSnapshot:
    return AlphaPriceSnapshot(
        netuid=NETUID,
        block=WINDOW_START_BLOCK + offset,
        price_tao_per_alpha=price,
        tao_reserve_rao=reserve,
    )


def _series(pattern: SparsePattern) -> tuple[AlphaPriceSnapshot, ...]:
    return tuple(
        _snapshot(
            offset,
            Decimal("1.00") + (Decimal(index) / Decimal(100)),
            1_000_000_000 + index * 1_000,
        )
        for index, offset in enumerate(pattern.offsets)
    )


def _from_intervals(first_offset: int, intervals: tuple[int, ...]) -> tuple[int, ...]:
    offsets = [first_offset]
    for interval in intervals:
        offsets.append(offsets[-1] + interval)
    return tuple(offsets)


_TEN_EXACT_THEN_NINE_GAPS = (CADENCE,) * 10 + (2_600,) * 9

PATTERNS = (
    SparsePattern(
        name="leading_gap",
        offsets=_from_intervals(5 * CADENCE, _TEN_EXACT_THEN_NINE_GAPS),
        resolver_volatility_status="resolved",
    ),
    SparsePattern(
        name="trailing_gap",
        offsets=_from_intervals(CADENCE, _TEN_EXACT_THEN_NINE_GAPS),
        resolver_volatility_status="resolved",
    ),
    SparsePattern(
        name="alternating_gaps",
        offsets=_from_intervals(
            CADENCE,
            tuple(CADENCE if index % 2 == 0 else 2_600 for index in range(19)),
        ),
        resolver_volatility_status="resolved",
    ),
    SparsePattern(
        name="mid_window_gap",
        offsets=_from_intervals(
            CADENCE,
            tuple(3_000 if index == 21 else CADENCE for index in range(44)),
        ),
        resolver_volatility_status="resolved",
    ),
    SparsePattern(
        name="dense_control",
        offsets=tuple((index + 1) * CADENCE for index in range(49)),
        resolver_volatility_status="resolved",
    ),
)

KNIFE_EDGE_PATTERN = SparsePattern(
    name="volatility_knife_edge",
    offsets=_from_intervals(CADENCE, (CADENCE,) + (1_600,) * 18),
    resolver_volatility_status="voided",
)


def _price_rao(snapshot: AlphaPriceSnapshot) -> Decimal:
    return snapshot.price_tao_per_alpha * Decimal(1_000_000_000)


def _expected_capped_weighted(
    series: tuple[AlphaPriceSnapshot, ...],
    value: Callable[[AlphaPriceSnapshot], Decimal],
) -> int:
    with localcontext(TR_CONTEXT):
        previous_block = WINDOW_START_BLOCK
        weighted_sum = Decimal(0)
        total_weight = 0
        for snapshot in series:
            span = snapshot.block - previous_block
            weight = min(span, CADENCE)
            weighted_sum += value(snapshot) * Decimal(weight)
            total_weight += weight
            previous_block = snapshot.block
        return int((weighted_sum / Decimal(total_weight)).quantize(Decimal("1")))


@pytest.mark.parametrize(
    "pattern", PATTERNS + (KNIFE_EDGE_PATTERN,), ids=lambda p: p.name
)
def test_sparse_band_voiding_and_estimators(pattern: SparsePattern) -> None:
    series = _series(pattern)
    assert MIN_REALIZED_WINDOW_SNAPSHOTS <= len(series) < 50
    assert not should_void_realized_window(series, horizon_blocks=HORIZON_BLOCKS)
    assert (
        filter_snapshots_for_window(
            series, window_start_block=WINDOW_START_BLOCK, horizon_blocks=HORIZON_BLOCKS
        )
        == series
    )
    assert max_drawdown_bps(series) == 0

    if pattern.resolver_volatility_status == "voided":
        with pytest.raises(AlphaMarketDataError, match="exact-cadence"):
            realized_volatility_bps(series)
    else:
        assert realized_volatility_bps(series) > 0

    assert twap_price_rao(series, window_start_block=WINDOW_START_BLOCK) == (
        _expected_capped_weighted(series, _price_rao)
    )
    assert liquidity_depth_rao(series, window_start_block=WINDOW_START_BLOCK) == (
        _expected_capped_weighted(
            series, lambda snapshot: Decimal(snapshot.tao_reserve_rao)
        )
    )


def test_leading_gap_weight_is_capped_instead_of_backfilled() -> None:
    series = tuple(
        _snapshot(
            _from_intervals(5 * CADENCE, _TEN_EXACT_THEN_NINE_GAPS)[index],
            Decimal("1.00") if index == 0 else Decimal("10.00"),
            100 if index == 0 else 1_000,
        )
        for index in range(20)
    )

    assert not should_void_realized_window(series, horizon_blocks=HORIZON_BLOCKS)
    assert (
        twap_price_rao(series, window_start_block=WINDOW_START_BLOCK) == 9_550_000_000
    )
    assert liquidity_depth_rao(series, window_start_block=WINDOW_START_BLOCK) == 955


def test_dense_control_weight_cap_is_identity() -> None:
    series = _series(PATTERNS[-1])

    assert twap_price_rao(series, window_start_block=WINDOW_START_BLOCK) == (
        _expected_capped_weighted(series, _price_rao)
    )
    assert liquidity_depth_rao(series, window_start_block=WINDOW_START_BLOCK) == (
        _expected_capped_weighted(
            series, lambda snapshot: Decimal(snapshot.tao_reserve_rao)
        )
    )


def test_sparse_resolver_voids_only_low_pair_volatility(storage: Storage) -> None:
    pattern = KNIFE_EDGE_PATTERN
    provider = FixtureAlphaPriceProvider(
        series_by_netuid={
            NETUID: AlphaPriceSeries(
                source="sparse_fixture", netuid=NETUID, snapshots=_series(pattern)
            )
        }
    )
    storage.open_round(
        windows=compute_windows(date(2026, 7, 8), offsets=DEFAULT_OFFSETS),
        schema_id=RISK_SCHEMA_ID,
        universe=StaticAlphaRiskUniverseProvider(netuids=(NETUID,)).fetch_universe(
            ROUND
        ),
        now_iso=NOW,
    )
    orchestrator = RiskScoringOrchestrator(
        storage=storage,
        price_provider=provider,
        half_life_rounds=2,
        reveal_close_block=lambda reveal_close: WINDOW_START_BLOCK,
    )

    orchestrator.resolve_and_score(ROUND, HORIZON_5D_SECONDS, now_iso=NOW)

    targets = {
        AssessmentCoordinate.subnet_asset(
            netuid=NETUID,
            horizon_seconds=HORIZON_5D_SECONDS,
            output=target.coordinate.output,
        ): target
        for target in storage.assessment_realized_targets_for(ROUND, RISK_SCHEMA_ID)
    }
    assert (
        targets[
            AssessmentCoordinate.subnet_asset(
                netuid=NETUID,
                horizon_seconds=HORIZON_5D_SECONDS,
                output=RiskOutput.REALIZED_VOLATILITY.value,
            )
        ].status
        == "voided"
    )
    assert {
        target.status
        for coordinate, target in targets.items()
        if coordinate.output != RiskOutput.REALIZED_VOLATILITY.value
    } == {"resolved"}
