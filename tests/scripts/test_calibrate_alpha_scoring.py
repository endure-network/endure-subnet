from __future__ import annotations

import json
from pathlib import Path

from scripts.calibrate_alpha_scoring import build_report


def test_alpha_calibration_report_matches_committed_artifact() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    committed = json.loads(
        (repo_root / "docs/calibration/alpha-risk-v1-rc1.json").read_text(
            encoding="utf-8"
        )
    )

    assert build_report() == committed
    assert committed["strategy_mean_scores"] == {
        "perfect": "1",
        "previous_round_persistence": "0.8222057314270187794571085144",
        "reference_baseline": "0.8213498025526932020280850862",
    }
    assert (
        committed["weight_scenarios_against_one_perfect_miner"]["reference_baseline"][
            "3"
        ]
        == "0.6243829436179768627897920409"
    )
