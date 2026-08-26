"""Reference lending miner baseline (Forge lending scope spec §M1).

Builds the deterministic, deliberately conservative seven-output submission a
reference miner pushes for every whitelisted netuid. Real miners are expected
to replace these values with their own risk models; the baseline exists so the
lending commit/reveal path can be exercised end-to-end with a bundle that is
always schema-valid.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from endure.assessment.schemas.forge_lending import (
    FORGE_LENDING_SCHEMA_ID,
    LENDING_HORIZON_SECONDS,
    LENDING_SPECS_BY_OUTPUT,
    ONE_E18,
    LendingAssetSubmission,
    LendingOutput,
    LendingOutputValue,
    LendingSubmissionBundle,
)
from endure.protocol.bundles import AssembledSubmission, assemble_bundle

BASELINE_REASON_CODE = "baseline_conservative"

# Conservative on every output's lenient side: a low oracle cross-check price,
# a 50%/60% LTV pair, a generous 10% liquidation bonus, closed caps, and the
# highest risk tier. Only collateral_factor is scored live in V1, so the
# baseline optimizes for validity and safety, not score.
BASELINE_LENDING_VALUES: Mapping[LendingOutput, int] = {
    LendingOutput.SAFE_ASSET_PRICE: ONE_E18 // 100,
    LendingOutput.COLLATERAL_FACTOR: 5000,
    LendingOutput.LIQUIDATION_THRESHOLD: 6000,
    LendingOutput.LIQUIDATION_INCENTIVE: ONE_E18 + ONE_E18 // 10,
    LendingOutput.SUPPLY_CAP: 0,
    LendingOutput.BORROW_CAP: 0,
    LendingOutput.RISK_TIER: 6,
}


def baseline_lending_bundle(
    *, round_id: str, netuids: Sequence[int]
) -> LendingSubmissionBundle:
    """Build the deterministic baseline bundle for one round and universe."""
    assets = tuple(
        LendingAssetSubmission(
            netuid=netuid,
            outputs=tuple(
                LendingOutputValue(
                    output=output,
                    value=value,
                    confidence_bps=LENDING_SPECS_BY_OUTPUT[output].confidence_floor_bps,
                    reason_codes=(BASELINE_REASON_CODE,),
                    horizon_seconds=LENDING_HORIZON_SECONDS,
                    unit=LENDING_SPECS_BY_OUTPUT[output].unit,
                )
                for output, value in BASELINE_LENDING_VALUES.items()
            ),
        )
        for netuid in sorted(set(netuids))
    )
    return LendingSubmissionBundle(
        round_id=round_id,
        schema_id=FORGE_LENDING_SCHEMA_ID,
        assets=assets,
    )


@dataclass(frozen=True, slots=True)
class LendingBaselineAssembler:
    """Assemble the deterministic baseline bundle for one lending round."""

    netuids: tuple[int, ...]
    miner_hotkey: str

    @property
    def schema_id(self) -> str:
        # A property, not a field: baseline_lending_bundle hardcodes the
        # lending schema inside the hashed bundle, so the wire value must not
        # be independently configurable.
        return FORGE_LENDING_SCHEMA_ID

    def __call__(self, round_id: str) -> AssembledSubmission:
        return assemble_bundle(
            baseline_lending_bundle(round_id=round_id, netuids=self.netuids),
            miner_hotkey=self.miner_hotkey,
        )
