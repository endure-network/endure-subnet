from __future__ import annotations

from decimal import Decimal

import pytest

from endure.scoring.lending.observables import collateral_factor_optimal_bps


class TestCollateralFactorOptimal:
    def test_forward_window_worst_case_retained_value_minus_buffer(self) -> None:
        prices = [Decimal(100), Decimal(70), Decimal(40), Decimal(55)]
        assert collateral_factor_optimal_bps(prices, liquidation_buffer_bps=500) == 3500

    def test_no_drawdown_caps_at_full_minus_buffer(self) -> None:
        prices = [Decimal(100), Decimal(120)]
        assert collateral_factor_optimal_bps(prices, liquidation_buffer_bps=500) == 9500

    def test_total_wipeout_floors_at_zero(self) -> None:
        prices = [Decimal(100), Decimal(1)]
        assert collateral_factor_optimal_bps(prices, liquidation_buffer_bps=500) == 0

    def test_empty_prices_rejected(self) -> None:
        with pytest.raises(ValueError):
            collateral_factor_optimal_bps([], liquidation_buffer_bps=500)

    def test_single_entry_price_rejected(self) -> None:
        with pytest.raises(ValueError):
            collateral_factor_optimal_bps([Decimal(100)], liquidation_buffer_bps=500)

    def test_non_positive_forward_price_rejected(self) -> None:
        with pytest.raises(ValueError):
            collateral_factor_optimal_bps(
                [Decimal(100), Decimal(0)], liquidation_buffer_bps=500
            )

    def test_negative_liquidation_buffer_rejected(self) -> None:
        with pytest.raises(ValueError):
            collateral_factor_optimal_bps(
                [Decimal(100), Decimal(90)], liquidation_buffer_bps=-1
            )

    def test_fractional_retained_bps_uses_half_even_rounding(self) -> None:
        prices = [Decimal("100"), Decimal("66.675")]
        assert collateral_factor_optimal_bps(prices, liquidation_buffer_bps=0) == 6668
