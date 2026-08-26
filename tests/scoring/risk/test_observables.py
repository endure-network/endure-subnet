from __future__ import annotations

from collections.abc import Callable
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

import pytest

from endure.scoring.context import TR_CONTEXT
from endure.scoring.lending.market_data import AlphaPriceSnapshot
from endure.scoring.market_data import AlphaMarketDataError
from endure.scoring.risk.observables import (
    BLOCK_SECONDS,
    CANONICAL_ALPHA_SNAPSHOT_CADENCE_BLOCKS,
    CANONICAL_ALPHA_SNAPSHOT_CADENCE_SECONDS,
    _block_weighted_decimal,
    filter_snapshots_for_window,
    horizon_seconds_to_blocks,
    liquidity_depth_rao,
    max_drawdown_bps,
    realized_volatility_bps,
    should_void_realized_window,
    twap_price_rao,
)


def _snapshot(block: int, price: str, reserve: int) -> AlphaPriceSnapshot:
    return AlphaPriceSnapshot(
        netuid=44,
        block=block,
        price_tao_per_alpha=Decimal(price),
        tao_reserve_rao=reserve,
    )


def _series(
    count: int, *, start: int = 100, step: int = 10
) -> tuple[AlphaPriceSnapshot, ...]:
    return tuple(
        _snapshot(start + index * step, "1.00", 1_000 + index) for index in range(count)
    )


class TestRiskObservableGoldenValues:
    def test_max_drawdown_uses_running_peak_and_half_even_quantization(self) -> None:
        # Given prices 100 → 80 → 120 → 90, the worst peak-to-trough drop is
        # 120 → 90 = 25%, so risk scope §Realized-value estimators returns 2500 bps.
        series = (
            _snapshot(10, "1.00", 1_000),
            _snapshot(20, "0.80", 1_000),
            _snapshot(30, "1.20", 1_000),
            _snapshot(40, "0.90", 1_000),
        )

        assert max_drawdown_bps(series) == 2500

    def test_max_drawdown_is_floored_at_zero_for_monotonic_up(self) -> None:
        series = (_snapshot(10, "1.00", 1_000), _snapshot(20, "1.10", 1_000))

        assert max_drawdown_bps(series) == 0

    def test_max_drawdown_matches_bruteforce_on_fixed_randomish_series(self) -> None:
        series = tuple(
            _snapshot(100 + index, price, 1_000)
            for index, price in enumerate(
                ("1.00", "0.97", "1.04", "0.88", "0.91", "1.10", "0.99")
            )
        )
        with localcontext(TR_CONTEXT):
            brute = max(
                (Decimal(1) - later.price_tao_per_alpha / earlier.price_tao_per_alpha)
                for earlier_index, earlier in enumerate(series)
                for later in series[earlier_index:]
            )
            expected = int(
                (brute * Decimal(10000)).quantize(
                    Decimal("1"), rounding=ROUND_HALF_EVEN
                )
            )

        assert max_drawdown_bps(series) == expected

    def test_realized_volatility_uses_decimal_log_returns_and_canonical_cadence(
        self,
    ) -> None:
        # Given ten exact-cadence log returns alternating ln(1.10) and ln(0.90).
        prices = [Decimal("1.00")]
        returns = (Decimal("1.10"), Decimal("0.90")) * 5
        for item in returns:
            prices.append(prices[-1] * item)
        series = tuple(
            _snapshot(
                10 + index * CANONICAL_ALPHA_SNAPSHOT_CADENCE_BLOCKS,
                str(price),
                1_000,
            )
            for index, price in enumerate(prices)
        )
        with localcontext(TR_CONTEXT):
            log_returns = tuple(item.ln() for item in returns)
            mean = sum(log_returns, Decimal(0)) / Decimal(len(log_returns))
            variance = sum((item - mean) ** 2 for item in log_returns) / Decimal(
                len(log_returns)
            )
            periods_per_year = Decimal(365 * 24 * 60 * 60) / Decimal(
                CANONICAL_ALPHA_SNAPSHOT_CADENCE_SECONDS
            )
            expected = int(
                (variance.sqrt() * periods_per_year.sqrt() * Decimal(10000)).quantize(
                    Decimal("1"), rounding=ROUND_HALF_EVEN
                )
            )

        assert CANONICAL_ALPHA_SNAPSHOT_CADENCE_BLOCKS * BLOCK_SECONDS == 7_200
        assert periods_per_year == Decimal(4_380)
        assert realized_volatility_bps(series) == expected

    def test_realized_volatility_skips_gap_spanning_pairs(self) -> None:
        # Given: ten exact-cadence ln(1.10) pairs plus one skipped two-cadence gap.
        snapshots = []
        block = 0
        price = Decimal("1.00")
        for index in range(12):
            snapshots.append(_snapshot(block, str(price), 1_000))
            if index == 5:
                block += 2 * CANONICAL_ALPHA_SNAPSHOT_CADENCE_BLOCKS
                price *= Decimal("0.99")
            else:
                block += CANONICAL_ALPHA_SNAPSHOT_CADENCE_BLOCKS
                price *= Decimal("1.10")
        series = tuple(snapshots)

        # When / Then: only the ten exact-cadence ln(1.10) returns are annualized.
        assert realized_volatility_bps(series) == 0

    def test_realized_volatility_raises_when_no_exact_cadence_pairs(self) -> None:
        series = (
            _snapshot(0, "1.00", 1_000),
            _snapshot(2 * CANONICAL_ALPHA_SNAPSHOT_CADENCE_BLOCKS, "1.10", 1_000),
        )

        with pytest.raises(AlphaMarketDataError, match="exact-cadence"):
            realized_volatility_bps(series)

    def test_realized_volatility_raises_when_too_few_snapshots(self) -> None:
        with pytest.raises(AlphaMarketDataError, match="exact-cadence"):
            realized_volatility_bps((_snapshot(0, "1.00", 1_000),))

    def test_twap_and_liquidity_depth_are_block_weighted_from_window_start(
        self,
    ) -> None:
        # Convention: snapshot i covers (previous_block, block_i].
        # Weights are 10, 30, 60 blocks. Price RAO: (1*10 + 2*30 + 4*60)/100 * 1e9;
        # reserve RAO: (100*10 + 200*30 + 300*60)/100.
        series = (
            _snapshot(100, "1.00", 100),
            _snapshot(130, "2.00", 200),
            _snapshot(190, "4.00", 300),
        )

        assert twap_price_rao(series, window_start_block=90) == 3_100_000_000
        assert liquidity_depth_rao(series, window_start_block=90) == 250

    def test_twap_and_depth_weight_terminal_snapshot_backward_from_window_start(
        self,
    ) -> None:
        # Given a half-open window (100, 160], backward weighting assigns spans
        # 10, 30, 20 to the values at 110, 140, 160. The terminal 9.00 value must
        # contribute: price = (1*10 + 2*30 + 9*20)/60 = 4.166666667 TAO.
        series = (
            _snapshot(110, "1.00", 100),
            _snapshot(140, "2.00", 200),
            _snapshot(160, "9.00", 900),
        )

        assert twap_price_rao(series, window_start_block=100) == 4_166_666_667
        assert liquidity_depth_rao(series, window_start_block=100) == 417

    def test_gappy_block_weighting_is_not_cadence_weighting(self) -> None:
        # Equal-snapshot averaging would be 2e9; block weighting gives the gap
        # closed by the second snapshot most of the mass: (1*10 + 3*90)/100 = 2.8.
        series = (_snapshot(100, "1.00", 100), _snapshot(190, "3.00", 300))

        assert twap_price_rao(series, window_start_block=90) == 2_800_000_000
        assert liquidity_depth_rao(series, window_start_block=90) == 280


class TestRiskWindowAndVoiding:
    def test_filter_uses_half_open_window_excluding_start_including_end(self) -> None:
        series = (
            _snapshot(100, "1", 1),
            _snapshot(101, "2", 2),
            _snapshot(110, "3", 3),
            _snapshot(111, "4", 4),
        )

        filtered = filter_snapshots_for_window(
            series, window_start_block=100, horizon_blocks=10
        )

        assert tuple(snapshot.block for snapshot in filtered) == (101, 110)

    def test_voids_with_fewer_than_twenty_snapshots(self) -> None:
        assert should_void_realized_window(_series(19), horizon_blocks=100)

    def test_exactly_twenty_snapshots_and_eighty_percent_span_is_not_void(self) -> None:
        series = _series(20, start=100, step=4)
        series = (*series[:-1], _snapshot(180, "1.00", 1_000))

        assert not should_void_realized_window(series, horizon_blocks=100)

    def test_one_block_less_than_eighty_percent_span_is_void(self) -> None:
        series = _series(20, start=100, step=4)
        series = (*series[:-1], _snapshot(179, "1.00", 1_000))

        assert should_void_realized_window(series, horizon_blocks=100)


class TestRiskObservableGuards:
    """Every estimator rejects an unusable window instead of scoring garbage."""

    @pytest.mark.parametrize(
        ("call", "message"),
        [
            pytest.param(
                lambda: horizon_seconds_to_blocks(0),
                "horizon_seconds must be positive",
                id="horizon-not-positive",
            ),
            pytest.param(
                lambda: horizon_seconds_to_blocks(BLOCK_SECONDS + 1),
                "horizon_seconds must align to 12s blocks",
                id="horizon-off-block-grid",
            ),
            pytest.param(
                lambda: filter_snapshots_for_window(
                    _series(3), window_start_block=0, horizon_blocks=0
                ),
                "horizon_blocks must be positive",
                id="window-horizon-not-positive",
            ),
            pytest.param(
                lambda: max_drawdown_bps(()),
                "drawdown series must not be empty",
                id="drawdown-empty",
            ),
            pytest.param(
                lambda: realized_volatility_bps(_series(2), cadence_blocks=0),
                "cadence_blocks must be positive",
                id="volatility-cadence-not-positive",
            ),
            pytest.param(
                lambda: twap_price_rao((), window_start_block=0),
                "weighted series must not be empty",
                id="weighted-empty",
            ),
            pytest.param(
                lambda: _block_weighted_decimal(
                    _series(2), window_start_block=0, values=(Decimal(1),)
                ),
                "weighted values must match snapshots",
                id="weighted-values-length-mismatch",
            ),
            pytest.param(
                lambda: twap_price_rao(_series(2), window_start_block=100),
                "first weighted snapshot must follow window_start_block",
                id="weighted-starts-on-window-open",
            ),
            pytest.param(
                lambda: liquidity_depth_rao(
                    (_snapshot(120, "1.00", 10), _snapshot(110, "1.00", 10)),
                    window_start_block=100,
                ),
                "price snapshots must be ascending by block",
                id="weighted-descending-blocks",
            ),
        ],
    )
    def test_unusable_window_raises_market_data_error(
        self, call: Callable[[], object], message: str
    ) -> None:
        with pytest.raises(AlphaMarketDataError, match=message):
            call()

    def test_volatility_needs_enough_exact_cadence_returns(self) -> None:
        # Snapshots exist but none are exactly one cadence apart, so no return
        # series can be formed and the estimator must void rather than
        # annualize a single stale pair.
        off_cadence = tuple(
            _snapshot(
                100 + index * (CANONICAL_ALPHA_SNAPSHOT_CADENCE_BLOCKS + 1),
                "1.00",
                1_000,
            )
            for index in range(3)
        )

        with pytest.raises(AlphaMarketDataError, match="exact-cadence pairs"):
            realized_volatility_bps(off_cadence)
