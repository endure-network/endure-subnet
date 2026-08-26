"""Signed Alpha Risk consensus feed (risk scope spec §Consensus, §Derived risk tier)."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from endure.assessment.coordinates import AssessmentConsensusRow, AssessmentCoordinate
from endure.assessment.schemas.subnet_alpha_risk import (
    HORIZON_30D_SECONDS,
    RISK_SCHEMA_ID,
    RiskOutput,
)
from endure.assessment.subnet_alpha_universe import ALPHA_RISK_WHITELISTED_NETUIDS
from endure.protocol.canonical import canonical_bundle_bytes
from endure.publication.risk_tier import RiskTierInputs, derive_risk_tier
from endure.scoring.assessment_orchestrator import REALIZED_TARGET_RESOLVED
from endure.storage.repository import Storage

Signer = Callable[[bytes], bytes]
FEED_SCHEMA_VERSION = 1
TIER_OUTPUTS = (
    RiskOutput.MAX_DRAWDOWN.value,
    RiskOutput.REALIZED_VOLATILITY.value,
)


@dataclass(frozen=True, slots=True)
class _TierFeedContext:
    rounds_by_netuid: dict[int, str]
    rows_by_round: dict[str, dict[int, list[AssessmentConsensusRow]]]
    resolved_outputs_by_round: dict[str, set[AssessmentCoordinate]]
    meta_by_round: dict[str, dict[str, object] | None]


def _netuids_from_universe(storage: Storage, round_id: str | None) -> tuple[int, ...]:
    if round_id is None:
        return ALPHA_RISK_WHITELISTED_NETUIDS
    snapshot = storage.universe_for(round_id, RISK_SCHEMA_ID)
    if snapshot is None:
        return ALPHA_RISK_WHITELISTED_NETUIDS
    return tuple(int(member) for member in snapshot.tickers)


def _consensus_payload(row: AssessmentConsensusRow) -> dict[str, object]:
    return {
        "output": row.coordinate.output,
        "horizon_seconds": row.coordinate.horizon_value,
        "median": str(row.value),
        "mad": str(row.dispersion),
        "n_submitters": row.n_submitters,
    }


def _rows_by_netuid(
    rows: list[AssessmentConsensusRow],
) -> dict[int, list[AssessmentConsensusRow]]:
    grouped: dict[int, list[AssessmentConsensusRow]] = {}
    for row in rows:
        if row.coordinate.target_kind != "subnet_asset":
            continue
        grouped.setdefault(int(row.coordinate.target_id), []).append(row)
    return grouped


def _thirty_day_values(
    rows: list[AssessmentConsensusRow],
) -> tuple[Decimal | None, Decimal | None]:
    max_drawdown: Decimal | None = None
    realized_volatility: Decimal | None = None
    for row in rows:
        coordinate = row.coordinate
        if coordinate.horizon_value != HORIZON_30D_SECONDS:
            continue
        if coordinate.output == RiskOutput.MAX_DRAWDOWN.value:
            max_drawdown = row.value
        if coordinate.output == RiskOutput.REALIZED_VOLATILITY.value:
            realized_volatility = row.value
    return max_drawdown, realized_volatility


def _resolved_thirty_day_outputs(
    storage: Storage, round_id: str | None
) -> set[AssessmentCoordinate]:
    if round_id is None:
        return set()
    return {
        target.coordinate
        for target in storage.assessment_realized_targets_for_horizon(
            round_id, RISK_SCHEMA_ID, HORIZON_30D_SECONDS
        )
        if target.status == REALIZED_TARGET_RESOLVED and target.value is not None
    }


def _tier_inputs(
    *,
    netuid: int,
    rows: list[AssessmentConsensusRow],
    resolved_outputs: set[AssessmentCoordinate],
) -> RiskTierInputs:
    drawdown, volatility = _thirty_day_values(rows)
    drawdown_coordinate = AssessmentCoordinate.subnet_asset(
        netuid=netuid,
        horizon_seconds=HORIZON_30D_SECONDS,
        output=RiskOutput.MAX_DRAWDOWN.value,
    )
    volatility_coordinate = AssessmentCoordinate.subnet_asset(
        netuid=netuid,
        horizon_seconds=HORIZON_30D_SECONDS,
        output=RiskOutput.REALIZED_VOLATILITY.value,
    )
    return RiskTierInputs(
        max_drawdown_bps=drawdown,
        realized_volatility_bps=volatility,
        max_drawdown_voided=drawdown_coordinate not in resolved_outputs,
        realized_volatility_voided=volatility_coordinate not in resolved_outputs,
    )


def _feed_payload(storage: Storage) -> dict[str, object]:
    round_id = storage.latest_assessment_consensus_round(RISK_SCHEMA_ID)
    consensus_rows = (
        []
        if round_id is None
        else storage.assessment_consensus_for(round_id, RISK_SCHEMA_ID)
    )
    consensus_netuids = tuple(
        netuid
        for netuid in _netuids_from_universe(storage, round_id)
        if netuid in ALPHA_RISK_WHITELISTED_NETUIDS
    )
    tier_rounds_by_netuid = {
        netuid: tier_round_id
        for netuid, tier_round_id in storage.latest_assessment_rounds_with_resolved_outputs(
            RISK_SCHEMA_ID,
            HORIZON_30D_SECONDS,
            TIER_OUTPUTS,
        ).items()
        if netuid in ALPHA_RISK_WHITELISTED_NETUIDS
    }
    tier_round_ids = tuple(sorted(set(tier_rounds_by_netuid.values())))
    tier_rows_by_round = {
        tier_round_id: _rows_by_netuid(
            storage.assessment_consensus_for(tier_round_id, RISK_SCHEMA_ID)
        )
        for tier_round_id in tier_round_ids
    }
    resolved_outputs_by_round = {
        tier_round_id: _resolved_thirty_day_outputs(storage, tier_round_id)
        for tier_round_id in tier_round_ids
    }
    tier_meta_by_round = {
        tier_round_id: storage.round_meta(tier_round_id, RISK_SCHEMA_ID)
        for tier_round_id in tier_round_ids
    }
    tier_context = _TierFeedContext(
        rounds_by_netuid=tier_rounds_by_netuid,
        rows_by_round=tier_rows_by_round,
        resolved_outputs_by_round=resolved_outputs_by_round,
        meta_by_round=tier_meta_by_round,
    )
    grouped_consensus_rows = _rows_by_netuid(consensus_rows)
    meta = None if round_id is None else storage.round_meta(round_id, RISK_SCHEMA_ID)
    return {
        "schema_id": RISK_SCHEMA_ID,
        "feed_schema_version": FEED_SCHEMA_VERSION,
        "round_id": round_id,
        "as_of": None if meta is None else meta["reveal_close_at"],
        "subnets": [
            _subnet_payload(
                netuid=netuid,
                consensus_rows=grouped_consensus_rows.get(netuid, []),
                tier_context=tier_context,
            )
            for netuid in sorted(set(consensus_netuids) | set(tier_rounds_by_netuid))
        ],
    }


def _subnet_payload(
    *,
    netuid: int,
    consensus_rows: list[AssessmentConsensusRow],
    tier_context: _TierFeedContext,
) -> dict[str, object]:
    tier_round_id = tier_context.rounds_by_netuid.get(netuid)
    if tier_round_id is None:
        tier = "unrated"
        tier_as_of = None
    else:
        tier = derive_risk_tier(
            _tier_inputs(
                netuid=netuid,
                rows=tier_context.rows_by_round[tier_round_id].get(netuid, []),
                resolved_outputs=tier_context.resolved_outputs_by_round[tier_round_id],
            )
        )
        tier_meta = tier_context.meta_by_round[tier_round_id]
        tier_as_of = None if tier_meta is None else tier_meta["reveal_close_at"]
    return {
        "netuid": netuid,
        "tier": tier,
        "tier_round_id": tier_round_id,
        "tier_as_of": tier_as_of,
        "consensus": [_consensus_payload(row) for row in consensus_rows],
    }


def build_signed_risk_feed(
    storage: Storage,
    *,
    signer: Signer | None = None,
    signed_by: str | None = None,
) -> dict[str, object]:
    payload = _feed_payload(storage)
    canonical_payload = canonical_bundle_bytes(payload)
    signature = None
    if signer is not None and signed_by is not None:
        signature = {
            "signed_by": signed_by,
            "signature_hex": signer(canonical_payload).hex(),
            "canonical_payload_sha256": hashlib.sha256(canonical_payload).hexdigest(),
        }
    return {"payload": payload, "signature": signature}
