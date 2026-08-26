"""Schema-neutral assessment coordinates for Batch 2A storage decisions."""

from __future__ import annotations

from decimal import Decimal

import pytest

from endure.assessment.coordinates import AssessmentConsensusRow, AssessmentCoordinate
from endure.assessment.schemas.forge_lending import (
    LENDING_HORIZON_SECONDS,
    LendingOutput,
)


class TestAssessmentCoordinate:
    def test_represent_one_lending_round_for_all_outputs(self) -> None:
        rows = [
            AssessmentConsensusRow(
                coordinate=AssessmentCoordinate.subnet_asset(
                    netuid=42,
                    horizon_seconds=LENDING_HORIZON_SECONDS,
                    output=output.value,
                ),
                value=Decimal(index + 1),
                dispersion=Decimal("0"),
                n_submitters=3,
            )
            for index, output in enumerate(LendingOutput)
        ]

        assert {row.coordinate.output for row in rows} == {
            output.value for output in LendingOutput
        }
        assert {
            (
                row.coordinate.target_kind,
                row.coordinate.target_id,
                row.coordinate.horizon_kind,
                row.coordinate.horizon_value,
            )
            for row in rows
        } == {("subnet_asset", "42", "seconds", LENDING_HORIZON_SECONDS)}

    def test_represent_tickers_without_changing_the_generic_shape(self) -> None:
        coordinate = AssessmentCoordinate.equity_ticker(
            ticker="WAL",
            horizon_trading_days=5,
            output="predicted_max_drawdown_bps",
        )

        assert coordinate.target_kind == "equity_ticker"
        assert coordinate.target_id == "WAL"
        assert coordinate.horizon_kind == "trading_days"
        assert coordinate.horizon_value == 5
        assert coordinate.output == "predicted_max_drawdown_bps"

    def test_rejects_invalid_coordinate_values(self) -> None:
        with pytest.raises(ValueError, match="netuid"):
            AssessmentCoordinate.subnet_asset(
                netuid=-1,
                horizon_seconds=LENDING_HORIZON_SECONDS,
                output=LendingOutput.COLLATERAL_FACTOR.value,
            )
        with pytest.raises(ValueError, match="horizon_value"):
            AssessmentCoordinate.subnet_asset(
                netuid=1,
                horizon_seconds=0,
                output=LendingOutput.COLLATERAL_FACTOR.value,
            )
        with pytest.raises(ValueError, match="output"):
            AssessmentCoordinate.subnet_asset(
                netuid=1,
                horizon_seconds=LENDING_HORIZON_SECONDS,
                output="",
            )


class TestAssessmentConsensusRow:
    def test_rejects_invalid_consensus_metadata(self) -> None:
        coordinate = AssessmentCoordinate.subnet_asset(
            netuid=1,
            horizon_seconds=LENDING_HORIZON_SECONDS,
            output=LendingOutput.COLLATERAL_FACTOR.value,
        )

        with pytest.raises(ValueError, match="dispersion"):
            AssessmentConsensusRow(
                coordinate=coordinate,
                value=Decimal("1"),
                dispersion=Decimal("-1"),
                n_submitters=1,
            )
        with pytest.raises(ValueError, match="n_submitters"):
            AssessmentConsensusRow(
                coordinate=coordinate,
                value=Decimal("1"),
                dispersion=Decimal("0"),
                n_submitters=0,
            )


class TestCoordinateOrdering:
    def test_coordinates_sort_by_declared_field_order(self) -> None:
        # The dataclass ordering is the single canonical coordinate ordering;
        # consensus row emission and the storage ORDER BY both mirror it.
        first = AssessmentCoordinate.subnet_asset(
            netuid=44, horizon_seconds=60, output="borrow_cap"
        )
        second = AssessmentCoordinate.subnet_asset(
            netuid=44, horizon_seconds=60, output="collateral_factor"
        )
        third = AssessmentCoordinate.subnet_asset(
            netuid=8, horizon_seconds=60, output="borrow_cap"
        )

        # target_id is compared as text ("44" < "8"), matching the SQL ORDER BY.
        assert sorted([third, second, first]) == [first, second, third]
