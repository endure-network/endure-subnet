"""Characterization lock for weighted median and MAD Decimal outputs."""

from __future__ import annotations

from decimal import Decimal

import pytest

from endure.aggregation.consensus import _weighted_mad, weighted_median


@pytest.mark.parametrize(
    ("values", "expected_median", "expected_mad"),
    [
        pytest.param(
            [(5, Decimal("1")), (5, Decimal("2")), (5, Decimal("3"))],
            Decimal("5"),
            Decimal("0"),
            id="deviation-zero",
        ),
        pytest.param(
            [(4, Decimal("1")), (5, Decimal("1")), (6, Decimal("1"))],
            Decimal("5"),
            Decimal("1"),
            id="deviation-one",
        ),
        pytest.param(
            [(-5, Decimal("1")), (0, Decimal("2")), (5, Decimal("3"))],
            Decimal("0"),
            Decimal("5"),
            id="cumulative-weight-tie-at-half",
        ),
        pytest.param(
            [(10, Decimal("2")), (20, Decimal("2"))],
            Decimal("10"),
            Decimal("0"),
            id="even-weight-split-selects-lower-value",
        ),
        pytest.param(
            [
                (0, Decimal("1")),
                (5, Decimal("1")),
                (6, Decimal("1")),
                (10, Decimal("1")),
            ],
            Decimal("5"),
            Decimal("1"),
            # Averaging the middle values would yield 5.5: exact deviations have
            # MAD 0.5, while int-truncated deviations have MAD 0. The current
            # weighted median is the integral sample 5, so truncation is lossless.
            id="fractional-midpoint-would-round-differently",
        ),
    ],
)
def test_weighted_median_and_mad_characterization(
    values: list[tuple[int, Decimal]],
    expected_median: Decimal,
    expected_mad: Decimal,
) -> None:
    median = weighted_median(values)
    mad = _weighted_mad(values, median)

    assert median == expected_median
    assert mad == expected_mad
