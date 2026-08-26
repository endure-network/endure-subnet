from __future__ import annotations

from pathlib import Path

from scripts.quality_gates.checks import (
    DECIMAL_POLICY_PATHS,
    find_decimal_policy_violations,
)


def test_decimal_policy_reports_float_calls(tmp_path: Path) -> None:
    target = tmp_path / "endure" / "protocol" / "example.py"
    target.parent.mkdir(parents=True)
    target.write_text("value = float('1.2')\n", encoding="utf-8")

    violations = find_decimal_policy_violations(
        repo_root=tmp_path, paths=(Path("endure/protocol"),)
    )

    assert len(violations) == 1
    assert "float()" in violations[0].message


def test_decimal_policy_reports_float_annotations(tmp_path: Path) -> None:
    target = tmp_path / "endure" / "scoring" / "example.py"
    target.parent.mkdir(parents=True)
    target.write_text("value: float = 1\n", encoding="utf-8")

    violations = find_decimal_policy_violations(
        repo_root=tmp_path, paths=(Path("endure/scoring"),)
    )

    assert len(violations) == 1
    assert "float annotations" in violations[0].message


def test_decimal_policy_ignores_clean_decimal_paths(tmp_path: Path) -> None:
    target = tmp_path / "endure" / "aggregation" / "example.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "from decimal import Decimal\nvalue = Decimal('1.0')\n", encoding="utf-8"
    )

    violations = find_decimal_policy_violations(
        repo_root=tmp_path, paths=(Path("endure/aggregation"),)
    )

    assert violations == []


def test_decimal_policy_reports_float_literals(tmp_path: Path) -> None:
    target = tmp_path / "endure" / "scoring" / "example.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 0.1\n", encoding="utf-8")

    violations = find_decimal_policy_violations(
        repo_root=tmp_path, paths=(Path("endure/scoring"),)
    )

    assert len(violations) == 1
    assert "float literals" in violations[0].message


def test_decimal_policy_reports_float_return_annotation(tmp_path: Path) -> None:
    target = tmp_path / "endure" / "scoring" / "example.py"
    target.parent.mkdir(parents=True)
    target.write_text("def rate() -> float: ...\n", encoding="utf-8")

    violations = find_decimal_policy_violations(
        repo_root=tmp_path, paths=(Path("endure/scoring"),)
    )

    assert len(violations) == 1
    assert "float annotations" in violations[0].message


def test_decimal_policy_reports_async_float_return_annotation(tmp_path: Path) -> None:
    target = tmp_path / "endure" / "scoring" / "example.py"
    target.parent.mkdir(parents=True)
    target.write_text("async def rate() -> float: ...\n", encoding="utf-8")

    violations = find_decimal_policy_violations(
        repo_root=tmp_path, paths=(Path("endure/scoring"),)
    )

    assert len(violations) == 1
    assert "float annotations" in violations[0].message


def test_decimal_policy_reports_subscripted_float_annotations(tmp_path: Path) -> None:
    target = tmp_path / "endure" / "scoring" / "example.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "values: list[float] = []\nmapping: dict[str, float] = {}\noptional: float | None = None\n",
        encoding="utf-8",
    )

    violations = find_decimal_policy_violations(
        repo_root=tmp_path, paths=(Path("endure/scoring"),)
    )

    assert len(violations) == 3
    assert all("float annotations" in violation.message for violation in violations)


def test_decimal_policy_reports_syntax_error_without_crashing(tmp_path: Path) -> None:
    target = tmp_path / "endure" / "scoring" / "broken.py"
    target.parent.mkdir(parents=True)
    target.write_text("def broken(:\n", encoding="utf-8")

    violations = find_decimal_policy_violations(
        repo_root=tmp_path, paths=(Path("endure/scoring"),)
    )

    assert len(violations) == 1
    assert "could not parse" in violations[0].message


def test_decimal_policy_targets_runtime_weight_paths() -> None:
    assert Path("endure/base/validator.py") in DECIMAL_POLICY_PATHS
    assert Path("endure/base/utils/weight_utils.py") in DECIMAL_POLICY_PATHS
