"""Reference Alpha Risk miner baseline.

Builds the deterministic R2 baseline submission for observed whitelist members.
The runtime supplies the narrow latest-observation provider; missing observations
are skipped and therefore receive the standard coverage penalty.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from endure.assessment.schemas.subnet_alpha_risk import (
    HORIZON_5D_SECONDS,
    HORIZON_30D_SECONDS,
    RISK_HORIZONS,
    RISK_SCHEMA_ID,
    RISK_SPECS_BY_OUTPUT,
    RiskAssetSubmission,
    RiskOutput,
    RiskOutputValue,
    RiskSubmissionBundle,
)
from endure.protocol.bundles import AssembledSubmission, assemble_bundle

BASELINE_REASON_CODE = "baseline_persistence"

# The deliberately simple public baseline uses constant bps outputs and the
# latest observed pool values for price and depth. It is not a modeled miner.
BASELINE_BPS_VALUES = {
    (RiskOutput.MAX_DRAWDOWN, HORIZON_5D_SECONDS): 1500,
    (RiskOutput.MAX_DRAWDOWN, HORIZON_30D_SECONDS): 3000,
    (RiskOutput.REALIZED_VOLATILITY, HORIZON_5D_SECONDS): 8000,
    (RiskOutput.REALIZED_VOLATILITY, HORIZON_30D_SECONDS): 8000,
}


@dataclass(frozen=True, slots=True)
class LatestPoolObservation:
    """Latest pool state needed for Alpha Risk persistence outputs."""

    price_rao: int
    tao_reserve_rao: int


LatestPoolObservationProvider = Callable[[int], LatestPoolObservation | None]


def _risk_output_value(output: RiskOutput, value: int, horizon: int) -> RiskOutputValue:
    spec = RISK_SPECS_BY_OUTPUT[output]
    return RiskOutputValue(
        output=output,
        value=value,
        confidence_bps=spec.confidence_floor_bps,
        reason_codes=(BASELINE_REASON_CODE,),
        horizon_seconds=horizon,
        unit=spec.unit,
    )


def _baseline_asset_outputs(
    observation: LatestPoolObservation,
) -> tuple[RiskOutputValue, ...]:
    outputs: list[RiskOutputValue] = []
    for horizon in RISK_HORIZONS:
        outputs.extend(
            (
                _risk_output_value(
                    RiskOutput.MAX_DRAWDOWN,
                    BASELINE_BPS_VALUES[(RiskOutput.MAX_DRAWDOWN, horizon)],
                    horizon,
                ),
                _risk_output_value(
                    RiskOutput.REALIZED_VOLATILITY,
                    BASELINE_BPS_VALUES[(RiskOutput.REALIZED_VOLATILITY, horizon)],
                    horizon,
                ),
                _risk_output_value(
                    RiskOutput.TWAP_PRICE, observation.price_rao, horizon
                ),
                _risk_output_value(
                    RiskOutput.LIQUIDITY_DEPTH,
                    observation.tao_reserve_rao,
                    horizon,
                ),
            )
        )
    return tuple(outputs)


def baseline_risk_bundle(
    *,
    round_id: str,
    netuids: Sequence[int],
    latest_observation: LatestPoolObservationProvider,
) -> RiskSubmissionBundle:
    """Build the deterministic Alpha Risk baseline bundle for one round."""
    assets: list[RiskAssetSubmission] = []
    for netuid in sorted(set(netuids)):
        observation = latest_observation(netuid)
        if observation is None:
            continue
        assets.append(
            RiskAssetSubmission(
                netuid=netuid,
                outputs=_baseline_asset_outputs(observation),
            )
        )
    return RiskSubmissionBundle(
        round_id=round_id, schema_id=RISK_SCHEMA_ID, assets=tuple(assets)
    )


@dataclass(frozen=True, slots=True)
class RiskBaselineAssembler:
    """Assemble the deterministic Alpha Risk baseline for one round."""

    netuids: tuple[int, ...]
    miner_hotkey: str
    latest_observation: LatestPoolObservationProvider

    @property
    def schema_id(self) -> str:
        return RISK_SCHEMA_ID

    def __call__(self, round_id: str) -> AssembledSubmission:
        return assemble_bundle(
            baseline_risk_bundle(
                round_id=round_id,
                netuids=self.netuids,
                latest_observation=self.latest_observation,
            ),
            miner_hotkey=self.miner_hotkey,
        )
