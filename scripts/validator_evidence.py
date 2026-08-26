"""Export and compare redacted Alpha Risk validator evidence.

The output is intentionally limited to consensus-relevant values. Secrets,
database URLs, signed bytes, and raw submission payloads never appear in the
export. Decimal values are serialized as text so two validators can be
compared without float conversion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from endure.assessment.coordinates import (
    AssessmentConsensusRow,
    AssessmentRealizedTarget,
)
from endure.assessment.schemas.subnet_alpha_risk import RISK_SCHEMA_ID
from endure.protocol.canonical import canonical_bundle_bytes
from endure.publication.risk_feed import build_signed_risk_feed
from endure.storage.repository import Storage

if __package__ in (None, "") and __name__ == "__main__":
    # Direct `python scripts/validator_evidence.py` puts scripts/ (not the repo
    # root) on sys.path; add the root so the sibling scripts package resolves.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.validator_evidence_compare import (  # noqa: E402
    InvalidEvidenceDocument,
    compare_validator_evidence,
    coordinate_key,
)

_EVIDENCE_LIMIT = 10_000


def _target_payload(target: AssessmentRealizedTarget) -> dict[str, object]:
    coordinate = target.coordinate
    return {
        "target_kind": coordinate.target_kind,
        "target_id": coordinate.target_id,
        "horizon_kind": coordinate.horizon_kind,
        "horizon_value": coordinate.horizon_value,
        "output": coordinate.output,
        "value": None if target.value is None else str(target.value),
        "status": str(target.status),
        "provider_payload_hash": target.provider_payload_hash,
    }


def _consensus_payload(row: AssessmentConsensusRow) -> dict[str, object]:
    coordinate = row.coordinate
    return {
        "target_kind": coordinate.target_kind,
        "target_id": coordinate.target_id,
        "horizon_kind": coordinate.horizon_kind,
        "horizon_value": coordinate.horizon_value,
        "output": coordinate.output,
        "value": str(row.value),
        "dispersion": str(row.dispersion),
        "n_submitters": row.n_submitters,
    }


def _round_payload(
    storage: Storage, schema_id: str, round_id: str
) -> dict[str, object]:
    meta = storage.round_meta(round_id, schema_id)
    if meta is None:
        raise RuntimeError(f"round disappeared while exporting: {round_id}")
    targets = [
        _target_payload(target)
        for target in storage.assessment_realized_targets_for(round_id, schema_id)
    ]
    consensus = [
        _consensus_payload(row)
        for row in storage.assessment_consensus_for(round_id, schema_id)
    ]
    targets.sort(key=coordinate_key)
    consensus.sort(key=coordinate_key)
    return {
        "round_id": round_id,
        "state": meta["state"],
        "created_at": meta["created_at"],
        "reveal_close_at": meta["reveal_close_at"],
        "universe_source_hash": meta["universe_source_hash"],
        "universe_size": meta["universe_size"],
        "accepted_submissions": meta["accepted_submissions"],
        # These remain null until the provenance fields are persisted. Keeping
        # them explicit prevents the evidence report from implying that block
        # agreement was proven when only payload hashes were compared.
        "start_block": None,
        "end_block": None,
        "block_hashes": [],
        "targets": targets,
        "consensus": consensus,
    }


def export_validator_evidence(
    db_url: str,
    schema_id: str,
    since_iso: str | None = None,
) -> dict[str, object]:
    """Export redacted, deterministically ordered evidence for one validator."""
    if schema_id != RISK_SCHEMA_ID:
        raise ValueError(f"evidence export supports {RISK_SCHEMA_ID!r} only")
    storage = Storage.from_url(db_url)
    rounds = storage.list_rounds(schema_id, limit=_EVIDENCE_LIMIT + 1)
    rounds_truncated = len(rounds) > _EVIDENCE_LIMIT
    rounds = rounds[:_EVIDENCE_LIMIT]
    if since_iso is not None:
        since = _parse_datetime(since_iso)
        rounds = [
            row for row in rounds if _parse_datetime(str(row["created_at"])) >= since
        ]
    round_payloads = [
        _round_payload(storage, schema_id, str(row["round_id"])) for row in rounds
    ]
    round_payloads.sort(key=lambda row: str(row["round_id"]))

    emas = [
        {
            "miner_hotkey": state.miner_hotkey,
            "target_kind": state.coordinate.target_kind,
            "target_id": state.coordinate.target_id,
            "horizon_kind": state.coordinate.horizon_kind,
            "horizon_value": state.coordinate.horizon_value,
            "output": state.coordinate.output,
            "ema": str(state.ema),
            "resolved_rounds": state.resolved_rounds,
        }
        for state in storage.assessment_ema_states(schema_id)
    ]
    emas.sort(
        key=lambda row: (
            str(row["miner_hotkey"]),
            str(row["target_id"]),
            str(row["horizon_value"]),
            str(row["output"]),
        )
    )
    weights = storage.weight_emission_history(schema_id, limit=_EVIDENCE_LIMIT + 1)
    weights_truncated = len(weights) > _EVIDENCE_LIMIT
    weights = weights[:_EVIDENCE_LIMIT]
    feed_payload = build_signed_risk_feed(storage)["payload"]
    feed_sha256 = hashlib.sha256(canonical_bundle_bytes(feed_payload)).hexdigest()
    return {
        "schema_id": schema_id,
        "rounds": round_payloads,
        "emas": emas,
        "weights": weights,
        "feed_sha256": feed_sha256,
        "truncated": {
            "rounds": rounds_truncated,
            "weights": weights_truncated,
        },
    }


def _parse_datetime(value: str) -> datetime:
    """Parse persisted timestamps and plain CLI dates as UTC datetimes."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--schema-id", default=RISK_SCHEMA_ID)
    parser.add_argument("--since", dest="since_iso")
    parser.add_argument("--compare", type=Path)
    arguments = parser.parse_args(argv)
    evidence = export_validator_evidence(
        arguments.database_url,
        arguments.schema_id,
        arguments.since_iso,
    )
    if arguments.compare is None:
        print(json.dumps(evidence, indent=2, sort_keys=True))
        return 0
    try:
        other = json.loads(arguments.compare.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        parser.error(f"cannot read comparison evidence: {error}")
    if not isinstance(other, dict):
        parser.error("comparison evidence must be a JSON object")
    try:
        diffs = compare_validator_evidence(evidence, other)
    except InvalidEvidenceDocument as error:
        parser.error(str(error))
    print(json.dumps(diffs, indent=2, sort_keys=True))
    return 1 if diffs else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
