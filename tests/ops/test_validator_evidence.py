from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from endure.assessment.registry import UniverseSnapshot
from endure.assessment.schemas.subnet_alpha_risk import RISK_SCHEMA_ID
from endure.protocol.round_engine import DEFAULT_OFFSETS, compute_windows
from scripts import validator_evidence as evidence_cli
from scripts.validator_evidence import (
    export_validator_evidence,
    main,
)

compare_validator_evidence = evidence_cli.compare_validator_evidence

ROUND = "2026-08-05"
CREATED_AT = "2026-08-05T12:00:00+00:00"


def test_direct_script_execution_resolves_sibling_imports() -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "validator_evidence.py"), "--help"],
        capture_output=True,
        text=True,
        cwd=str(root),
    )
    assert result.returncode == 0, result.stderr
    assert "ModuleNotFoundError" not in result.stderr


def test_identical_evidence_has_no_diffs() -> None:
    evidence = {
        "schema_id": RISK_SCHEMA_ID,
        "rounds": [{"round_id": "2026-08-05", "targets": []}],
        "emas": [{"miner_hotkey": "hk-a", "ema": "0.50"}],
        "weights": [{"status": "finalized", "weight_u16": 65535}],
        "feed_sha256": "abc",
    }

    assert compare_validator_evidence(evidence, evidence) == []


def test_diffs_name_round_and_consensus_key() -> None:
    left = {
        "schema_id": RISK_SCHEMA_ID,
        "rounds": [
            {
                "round_id": "2026-08-05",
                "targets": [{"target_id": "1", "output": "twap_price", "value": "10"}],
            }
        ],
        "emas": [],
        "weights": [],
        "feed_sha256": "abc",
    }
    right = {
        **left,
        "rounds": [
            {
                "round_id": "2026-08-05",
                "targets": [{"target_id": "1", "output": "twap_price", "value": "11"}],
            }
        ],
    }

    diffs = compare_validator_evidence(left, right)

    assert len(diffs) == 1
    assert diffs[0]["round_id"] == "2026-08-05"
    assert diffs[0]["key"] == "target_id=1/output=twap_price"


def test_round_comparison_ignores_validator_local_created_at() -> None:
    left = {
        "schema_id": RISK_SCHEMA_ID,
        "rounds": [
            {
                "round_id": ROUND,
                "created_at": "2026-08-05T12:00:00+00:00",
                "targets": [],
                "consensus": [],
            }
        ],
        "emas": [],
        "weights": [],
        "feed_sha256": "same",
    }
    right = {
        **left,
        "rounds": [
            {
                "round_id": ROUND,
                "created_at": "2026-08-05T12:00:07+00:00",
                "targets": [],
                "consensus": [],
            }
        ],
    }

    assert compare_validator_evidence(left, right) == []


def test_comparison_rejects_malformed_section_types() -> None:
    valid = {
        "schema_id": RISK_SCHEMA_ID,
        "rounds": [],
        "emas": [],
        "weights": [],
        "feed_sha256": "same",
    }

    with pytest.raises(ValueError, match="rounds must be a list"):
        compare_validator_evidence(valid, {**valid, "rounds": {}})


def test_coordinate_comparison_uses_full_coordinate_identity() -> None:
    subnet_target = {
        "target_kind": "subnet_asset",
        "target_id": "1",
        "horizon_kind": "seconds",
        "horizon_value": 300,
        "output": "twap_price",
        "value": "10",
    }
    equity_target = {
        **subnet_target,
        "target_kind": "equity_ticker",
        "horizon_kind": "trading_days",
    }
    left = {
        "schema_id": RISK_SCHEMA_ID,
        "rounds": [
            {
                "round_id": ROUND,
                "targets": [subnet_target, equity_target],
                "consensus": [],
            }
        ],
        "emas": [],
        "weights": [],
        "feed_sha256": "same",
    }
    right = {
        **left,
        "rounds": [
            {
                "round_id": ROUND,
                "targets": [equity_target],
                "consensus": [],
            }
        ],
    }

    diffs = compare_validator_evidence(left, right)

    assert len(diffs) == 1
    assert diffs[0]["scope"] == "targets"
    assert "target_kind=subnet_asset" in str(diffs[0]["key"])


def test_comparison_rejects_duplicate_coordinate_identity() -> None:
    target = {
        "target_kind": "subnet_asset",
        "target_id": "1",
        "horizon_kind": "seconds",
        "horizon_value": 300,
        "output": "twap_price",
        "value": "10",
    }
    evidence = {
        "schema_id": RISK_SCHEMA_ID,
        "rounds": [
            {
                "round_id": ROUND,
                "targets": [target, target],
                "consensus": [],
            }
        ],
        "emas": [],
        "weights": [],
        "feed_sha256": "same",
    }

    with pytest.raises(ValueError, match="duplicate targets identity"):
        compare_validator_evidence(evidence, evidence)


def test_round_comparison_distinguishes_missing_from_explicit_null() -> None:
    left = {
        "schema_id": RISK_SCHEMA_ID,
        "rounds": [
            {
                "round_id": ROUND,
                "end_block": None,
                "targets": [],
                "consensus": [],
            }
        ],
        "emas": [],
        "weights": [],
        "feed_sha256": "same",
    }
    right = {
        **left,
        "rounds": [{"round_id": ROUND, "targets": [], "consensus": []}],
    }

    diffs = compare_validator_evidence(left, right)

    assert len(diffs) == 1
    assert diffs[0]["key"] == "end_block"
    assert diffs[0]["left_present"] is True
    assert diffs[0]["right_present"] is False


def test_ema_comparison_uses_full_coordinate_identity() -> None:
    subnet_ema = {
        "miner_hotkey": "hk-a",
        "target_kind": "subnet_asset",
        "target_id": "1",
        "horizon_kind": "seconds",
        "horizon_value": 300,
        "output": "twap_price",
        "ema": "0.5",
    }
    equity_ema = {
        **subnet_ema,
        "target_kind": "equity_ticker",
        "horizon_kind": "trading_days",
    }
    left = {
        "schema_id": RISK_SCHEMA_ID,
        "rounds": [],
        "emas": [subnet_ema, equity_ema],
        "weights": [],
        "feed_sha256": "same",
    }
    right = {**left, "emas": [equity_ema]}

    diffs = compare_validator_evidence(left, right)

    assert len(diffs) == 1
    assert diffs[0]["scope"] == "emas"
    assert "target_kind=subnet_asset" in str(diffs[0]["key"])


def test_cli_rejects_non_object_comparison_document(
    storage, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    comparison_path = tmp_path / "comparison.json"
    comparison_path.write_text("[]", encoding="utf-8")

    with pytest.raises(SystemExit) as raised:
        main(
            [
                "--database-url",
                str(storage._engine.url),
                "--compare",
                str(comparison_path),
            ]
        )

    assert raised.value.code == 2
    assert "comparison evidence must be a JSON object" in capsys.readouterr().err


def test_cli_rejects_malformed_comparison_sections(
    storage, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    comparison = export_validator_evidence(str(storage._engine.url), RISK_SCHEMA_ID)
    comparison["rounds"] = {}
    comparison_path = tmp_path / "comparison.json"
    comparison_path.write_text(
        json.dumps(comparison),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as raised:
        main(
            [
                "--database-url",
                str(storage._engine.url),
                "--compare",
                str(comparison_path),
            ]
        )

    assert raised.value.code == 2
    assert "rounds must be a list" in capsys.readouterr().err


def test_export_empty_storage_is_redacted_and_json_safe(storage) -> None:
    evidence = export_validator_evidence(
        str(storage._engine.url),
        RISK_SCHEMA_ID,
    )

    assert evidence["schema_id"] == RISK_SCHEMA_ID
    assert evidence["rounds"] == []
    assert evidence["emas"] == []
    assert evidence["weights"] == []
    assert isinstance(evidence["feed_sha256"], str)
    assert "sqlite" not in str(evidence)


def test_export_populated_storage_includes_round_metadata(storage) -> None:
    storage.open_round(
        windows=compute_windows(date.fromisoformat(ROUND), offsets=DEFAULT_OFFSETS),
        schema_id=RISK_SCHEMA_ID,
        universe=UniverseSnapshot(
            round_id=ROUND,
            tickers=("1",),
            source_hash="source-hash",
        ),
        now_iso=CREATED_AT,
    )

    evidence = export_validator_evidence(str(storage._engine.url), RISK_SCHEMA_ID)

    rounds = evidence["rounds"]
    assert isinstance(rounds, list)
    assert rounds
    first_round = rounds[0]
    assert isinstance(first_round, dict)
    assert first_round["created_at"] == CREATED_AT


def test_export_since_accepts_plain_date(storage) -> None:
    storage.open_round(
        windows=compute_windows(date.fromisoformat(ROUND), offsets=DEFAULT_OFFSETS),
        schema_id=RISK_SCHEMA_ID,
        universe=None,
        now_iso=CREATED_AT,
    )

    evidence = export_validator_evidence(
        str(storage._engine.url), RISK_SCHEMA_ID, since_iso="2026-08-01"
    )

    rounds = evidence["rounds"]
    assert isinstance(rounds, list)
    assert all(isinstance(row, dict) for row in rounds)
    assert [row["round_id"] for row in rounds] == [ROUND]


def test_weight_comparison_ignores_local_batch_identity_and_names_row_diffs() -> None:
    left_weight_row = {
        "miner_hotkey": "hk-a",
        "uid": 0,
        "weight_processed_text": "0.5",
        "weight_u16": 32768,
        "emitted": True,
    }
    left_weight_batch = {
        "id": 1,
        "round_id": None,
        "block": 100,
        "attempted_at": "2026-08-05T12:00:00+00:00",
        "status": "finalized",
        "metagraph_size": 1,
        "rows": [left_weight_row],
    }
    left = {
        "schema_id": RISK_SCHEMA_ID,
        "rounds": [],
        "emas": [],
        "weights": [left_weight_batch],
        "feed_sha256": "same",
    }
    right = {
        **left,
        "weights": [
            {
                **left_weight_batch,
                "id": 99,
                "attempted_at": "2026-08-05T12:00:30+00:00",
                "rows": [
                    {
                        **left_weight_row,
                        "weight_u16": 32769,
                    }
                ],
            }
        ],
    }

    diffs = compare_validator_evidence(left, right)

    assert len(diffs) == 1
    assert diffs[0]["scope"] == "weights"
    key = diffs[0]["key"]
    assert isinstance(key, str)
    assert key.endswith("/uid=0")
    left_diff = diffs[0]["left"]
    right_diff = diffs[0]["right"]
    assert isinstance(left_diff, dict)
    assert isinstance(right_diff, dict)
    assert left_diff["weight_u16"] == 32768
    assert right_diff["weight_u16"] == 32769


def test_weight_comparison_reports_different_confirmation_states() -> None:
    submitted = {
        "schema_id": RISK_SCHEMA_ID,
        "rounds": [],
        "emas": [],
        "weights": [
            {
                "round_id": None,
                "block": 100,
                "status": "submitted",
                "confirmation_state": "submitted",
                "rows": [],
            }
        ],
        "feed_sha256": "same",
    }
    confirmed = {
        **submitted,
        "weights": [
            {
                "round_id": None,
                "block": 100,
                "status": "submitted",
                "confirmation_state": "confirmed",
                "rows": [],
            }
        ],
    }

    diffs = compare_validator_evidence(submitted, confirmed)

    assert len(diffs) == 1
    assert diffs[0]["scope"] == "weights"
    left_diff = diffs[0]["left"]
    right_diff = diffs[0]["right"]
    assert isinstance(left_diff, dict)
    assert isinstance(right_diff, dict)
    assert left_diff["confirmation_state"] == "submitted"
    assert right_diff["confirmation_state"] == "confirmed"


def test_weight_comparison_ignores_local_confirmation_timestamps() -> None:
    left = {
        "schema_id": RISK_SCHEMA_ID,
        "rounds": [],
        "emas": [],
        "weights": [
            {
                "round_id": None,
                "block": 100,
                "status": "submitted",
                "confirmation_state": "confirmed",
                "confirmed_at": "2026-08-09T00:00:00+00:00",
                "rows": [],
            }
        ],
        "feed_sha256": "same",
    }
    right = {
        **left,
        "weights": [
            {
                "round_id": None,
                "block": 100,
                "status": "submitted",
                "confirmation_state": "confirmed",
                "confirmed_at": "2026-08-09T00:00:01+00:00",
                "rows": [],
            }
        ],
    }

    assert compare_validator_evidence(left, right) == []


@pytest.mark.parametrize("value", [Decimal("0.1"), Decimal("1.00")])
def test_decimal_values_are_serialized_as_strings(value: Decimal) -> None:
    evidence = {
        "schema_id": RISK_SCHEMA_ID,
        "rounds": [],
        "emas": [{"ema": str(value)}],
        "weights": [],
        "feed_sha256": "abc",
    }

    assert compare_validator_evidence(evidence, evidence) == []
