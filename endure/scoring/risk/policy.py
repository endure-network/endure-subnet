"""Active Alpha Risk coordinates shared by scoring and payout displays."""

from endure.assessment.coordinates import AssessmentCoordinate
from endure.assessment.schemas.subnet_alpha_risk import RISK_HORIZONS, RiskOutput
from endure.assessment.subnet_alpha_universe import ALPHA_RISK_WHITELISTED_NETUIDS


def active_risk_coordinates(
    netuids: tuple[int, ...] = ALPHA_RISK_WHITELISTED_NETUIDS,
) -> frozenset[AssessmentCoordinate]:
    """Return the current payout and consensus coordinate set."""
    return frozenset(
        AssessmentCoordinate.subnet_asset(
            netuid=netuid, horizon_seconds=horizon, output=output.value
        )
        for netuid in netuids
        for horizon in RISK_HORIZONS
        for output in RiskOutput
    )
