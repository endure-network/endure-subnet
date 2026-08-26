"""Schema-neutral consensus over the coordinate spine (risk scope spec §R1)."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Protocol

from endure.aggregation.consensus import aggregate_cell, consensus_weight
from endure.assessment.coordinates import AssessmentConsensusRow, AssessmentCoordinate


class _WireOutput(Protocol):
    @property
    def value(self) -> str: ...


class _AssessmentOutputValue(Protocol):
    @property
    def horizon_seconds(self) -> int: ...

    @property
    def output(self) -> _WireOutput: ...

    @property
    def value(self) -> int: ...


class _AssessmentAsset(Protocol):
    @property
    def netuid(self) -> int: ...

    @property
    def outputs(self) -> tuple[_AssessmentOutputValue, ...]: ...


class AssessmentConsensusBundle(Protocol):
    @property
    def assets(self) -> tuple[_AssessmentAsset, ...]: ...


class AssessmentConsensusBundleModel(Protocol):
    """A registered bundle model, named by the shared consensus surface it parses to."""

    def model_validate_json(self, json_data: str, /) -> AssessmentConsensusBundle: ...


def compute_assessment_consensus(
    bundles: Mapping[str, AssessmentConsensusBundle],
    blends: Mapping[str, Decimal],
) -> list[AssessmentConsensusRow]:
    samples: dict[AssessmentCoordinate, list[tuple[int, Decimal]]] = {}
    for hotkey in sorted(bundles):
        weight = consensus_weight(blends, hotkey)
        for asset in bundles[hotkey].assets:
            for output_value in asset.outputs:
                coordinate = AssessmentCoordinate.subnet_asset(
                    netuid=asset.netuid,
                    horizon_seconds=output_value.horizon_seconds,
                    output=output_value.output.value,
                )
                samples.setdefault(coordinate, []).append((output_value.value, weight))

    rows: list[AssessmentConsensusRow] = []
    for coordinate in sorted(samples):
        median, dispersion, n_submitters = aggregate_cell(samples[coordinate])
        rows.append(
            AssessmentConsensusRow(
                coordinate=coordinate,
                value=median,
                dispersion=dispersion,
                n_submitters=n_submitters,
            )
        )
    return rows
