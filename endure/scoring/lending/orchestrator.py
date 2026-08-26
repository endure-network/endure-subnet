"""Lending scoring config for generic assessments (risk scope spec §R1)."""

from __future__ import annotations

from decimal import Decimal

import bittensor as bt
from pydantic import ValidationError

from endure.assessment.coordinates import AssessmentCoordinate, AssessmentRealizedTarget
from endure.assessment.lending_universe import parse_lending_universe_members
from endure.assessment.schemas.forge_lending import (
    COLLATERAL_FACTOR_LIQUIDATION_BUFFER_BPS,
    FORGE_LENDING_SCHEMA_ID,
    LENDING_HORIZON_SECONDS,
    LENDING_SPECS_BY_OUTPUT,
    LendingOutput,
    LendingSubmissionBundle,
)
from endure.scoring.assessment_orchestrator import (
    REALIZED_TARGET_RESOLVED,
    REALIZED_TARGET_VOIDED,
    AssessmentResolutionContext,
    AssessmentScoringConfig,
    AssessmentScoringOrchestrator,
    ScoredOutputConfig,
    submitted_assessment_values,
)
from endure.scoring.lending.market_data import AlphaPriceProvider, ResolutionWindow
from endure.scoring.lending.observables import collateral_factor_optimal_bps
from endure.storage.repository import Storage


def _cf_coordinate(netuid: int, horizon: int) -> AssessmentCoordinate:
    return AssessmentCoordinate.subnet_asset(
        netuid=netuid,
        horizon_seconds=horizon,
        output=LendingOutput.COLLATERAL_FACTOR.value,
    )


def _accepted_lending_values(
    storage: Storage, round_id: str
) -> dict[str, dict[AssessmentCoordinate, int]]:
    def coordinate_for(
        netuid: int, horizon: int, output: LendingOutput
    ) -> AssessmentCoordinate | None:
        if output is not LendingOutput.COLLATERAL_FACTOR:
            return None
        return _cf_coordinate(netuid, horizon)

    values: dict[str, dict[AssessmentCoordinate, int]] = {}
    for accepted in storage.accepted_assessment_bundles(
        round_id, FORGE_LENDING_SCHEMA_ID
    ):
        try:
            bundle = LendingSubmissionBundle.model_validate_json(accepted.bundle_json)
        except ValidationError as error:
            bt.logging.error(
                f"accepted lending bundle for {accepted.miner_hotkey} failed to parse "
                f"during scoring — skipping miner: {error}"
            )
            continue
        values[accepted.miner_hotkey] = submitted_assessment_values(
            bundle.assets, coordinate_for
        )
    return values


class LendingScoringOrchestrator(AssessmentScoringOrchestrator):
    """Thin Forge lending config over the generic assessment orchestrator."""

    def __init__(
        self,
        *,
        storage: Storage,
        price_provider: AlphaPriceProvider,
        half_life_rounds: int,
        liquidation_buffer_bps: int = COLLATERAL_FACTOR_LIQUIDATION_BUFFER_BPS,
    ) -> None:
        full_history_window = ResolutionWindow(start_block=0, horizon_blocks=2**63 - 1)

        def resolve_cf(
            _context: AssessmentResolutionContext, netuid: int, horizon: int
        ) -> AssessmentRealizedTarget:
            series = price_provider.price_series(netuid, window=full_history_window)
            if series is None:
                return AssessmentRealizedTarget(
                    coordinate=_cf_coordinate(netuid, horizon),
                    value=None,
                    status=REALIZED_TARGET_VOIDED,
                )
            target_bps = collateral_factor_optimal_bps(
                series.prices, liquidation_buffer_bps=liquidation_buffer_bps
            )
            return AssessmentRealizedTarget(
                coordinate=_cf_coordinate(netuid, horizon),
                value=Decimal(target_bps),
                status=REALIZED_TARGET_RESOLVED,
                provider_payload_hash=series.payload_hash,
            )

        super().__init__(
            storage=storage,
            config=AssessmentScoringConfig(
                schema_id=FORGE_LENDING_SCHEMA_ID,
                horizons=(LENDING_HORIZON_SECONDS,),
                universe_members=parse_lending_universe_members,
                accepted_values=lambda round_id: _accepted_lending_values(
                    storage, round_id
                ),
                outputs=(
                    ScoredOutputConfig(
                        output=LendingOutput.COLLATERAL_FACTOR.value,
                        resolver=resolve_cf,
                        spec=LENDING_SPECS_BY_OUTPUT[LendingOutput.COLLATERAL_FACTOR],
                    ),
                ),
            ),
            half_life_rounds=half_life_rounds,
        )
