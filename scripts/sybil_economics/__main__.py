"""Offline evidence: python -m scripts.sybil_economics --output report.json."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from endure.protocol.version_contract import CURRENT_VERSION_KEY
from endure.runtime.identity import content_revision
from endure.scoring.weight_processing import U16_MAX
from scripts.calibrate_alpha_scoring import build_report as historical_report
from scripts.sybil_economics.models import PublicSnapshot
from scripts.sybil_economics.report import (
    benchmark_rows,
    coordinated_rows,
    cost_rows,
    entry_rows,
    experiments,
    exposure_rows,
    lifecycle_rows,
    live_observations,
)
from scripts.sybil_economics.simulation import score_values

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "docs/investigations/evidence/2026-10-13-public-snapshot.json"


def build_report(input_path: Path = DEFAULT_INPUT) -> dict[str, object]:
    """No network, wallets, databases, or production mutations."""
    raw = input_path.read_bytes()
    snapshot = PublicSnapshot.model_validate_json(raw)
    if (
        snapshot.health.get("protocol_version_key") != CURRENT_VERSION_KEY
        or snapshot.health.get("content_revision") != content_revision()
    ):
        raise ValueError(
            "observed runtime policy identity differs from the analysis tree; "
            "keep deployment behavior unknown"
        )
    if (
        snapshot.chain.min_allowed_weights != 1
        or snapshot.chain.max_weight_limit_u16 != U16_MAX
    ):
        raise ValueError(
            "observed chain limits block this release's scored emission; do not substitute defaults"
        )
    fixture, live = experiments(ROOT, snapshot)
    historical = historical_report()
    committed = json.loads(
        (ROOT / "docs/calibration/alpha-risk-v1-rc1.json").read_text()
    )
    if historical != committed:
        raise ValueError("historical calibration failed to reproduce")
    return {
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "historical_calibration_exact": True,
        "historical_calibration": historical,
        "observed_policy": {
            "api_base": snapshot.api_base,
            "health": snapshot.health,
            "chain_block": snapshot.chain.block,
            "chain_hash": snapshot.chain.block_hash,
            "runtime_spec_version": snapshot.chain.spec_version,
            "burn_bps": snapshot.chain.burn_bps,
            "chain_min_allowed_weights": snapshot.chain.min_allowed_weights,
            "chain_max_weight_limit_u16": snapshot.chain.max_weight_limit_u16,
        },
        "live_observations": live_observations(snapshot),
        "experiments": exposure_rows(fixture, snapshot.chain)
        + exposure_rows(live, snapshot.chain),
        "synthetic_lifecycle": lifecycle_rows(fixture),
        "synthetic_coordinated_distortion": coordinated_rows(),
        "new_entrant_current_cohort_counterfactual": entry_rows(
            snapshot, score_values(live.cells, live.reference)
        ),
        "registration_and_conditional_break_even": cost_rows(fixture, snapshot.chain),
        "limits": [
            "Perfect competitors and correct-agreement values are oracle controls; they are not deployable forecasts.",
            "Static experiments are resolved-score/equal-history counterfactuals, not full validator database replays.",
            "UID assignment in encoding experiments is synthetic; observed chain limits are both one.",
            "No current score was substituted for publication-time reputation in a claimed historical publication replay.",
            "Observed duplicate groups do not establish ownership or malicious intent.",
            "Epoch incentive proportions are observed allocations, not proof of paid wallet returns or counterfactual Yuma outcomes.",
            "Numeric pooling tolerances and gamma12 are research parameters, not approved policy.",
            "Stale reuse assumes availability under the stored embargo schedule; contemporaneous public accessibility was not observed. Live operator seed forecasts were selected ex post by group size, not proven public before the original commitment.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--jsonl",
        action="store_true",
        help="inspectable archive: one record per experiment, plus section records",
    )
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="measure local allocation CPU; outputs are intentionally nondeterministic",
    )
    args = parser.parse_args()
    report = (
        build_report(args.input)
        if not args.benchmark
        else {
            "allocation_only_benchmarks": benchmark_rows(
                experiments(
                    ROOT, PublicSnapshot.model_validate_json(args.input.read_bytes())
                )[1]
            )
        }
    )
    if args.jsonl:
        records: list[dict[str, object]] = []
        for key, value in sorted(report.items()):
            if key == "experiments" and isinstance(value, list):
                records.extend(
                    {"section": key, "index": index, "data": row}
                    for index, row in enumerate(value)
                )
            else:
                records.append({"section": key, "data": value})
        rendered = "".join(json.dumps(row, sort_keys=True) + "\n" for row in records)
    else:
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
