"""Weighted-median consensus aggregation (spec §9)."""

from __future__ import annotations

from decimal import Decimal

from endure.aggregation.consensus import weighted_median


class TestWeightedMedian:
    def test_simple_median(self) -> None:
        values = [(-100, Decimal("1")), (-200, Decimal("1")), (-300, Decimal("1"))]

        assert weighted_median(values) == Decimal(-200)

    def test_weight_pulls_the_median(self) -> None:
        values = [(-100, Decimal("10")), (-200, Decimal("1")), (-300, Decimal("1"))]

        assert weighted_median(values) == Decimal(-100)

    def test_deterministic_for_ties(self) -> None:
        values = [(-100, Decimal("1")), (-200, Decimal("1"))]

        assert weighted_median(values) == weighted_median(list(reversed(values)))
