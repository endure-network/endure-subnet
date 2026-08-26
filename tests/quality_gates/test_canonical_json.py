from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.quality_gates.checks import find_noncanonical_json


def test_canonical_json_reports_invalid_json(tmp_path: Path) -> None:
    target = tmp_path / "sample.json"
    target.write_text('{"a": }\n', encoding="utf-8")

    violations = find_noncanonical_json(
        repo_root=tmp_path, paths=(Path("sample.json"),)
    )

    assert len(violations) == 1
    assert "invalid JSON" in violations[0].message


def test_canonical_json_reports_noncanonical_formatting(tmp_path: Path) -> None:
    target = tmp_path / "sample.json"
    target.write_text('{"b": 1, "a": 2}\n', encoding="utf-8")

    violations = find_noncanonical_json(
        repo_root=tmp_path, paths=(Path("sample.json"),)
    )

    assert len(violations) == 1
    assert "not canonicalized" in violations[0].message


def test_canonical_json_accepts_sorted_pretty_json(tmp_path: Path) -> None:
    target = tmp_path / "sample.json"
    target.write_text('{\n  "a": 2,\n  "b": 1\n}\n', encoding="utf-8")

    violations = find_noncanonical_json(
        repo_root=tmp_path, paths=(Path("sample.json"),)
    )

    assert violations == []


def test_canonical_json_ignores_generated_coverage_artifact(tmp_path: Path) -> None:
    target = tmp_path / "coverage.json"
    target.write_text('{"z": 1}\n', encoding="utf-8")

    violations = find_noncanonical_json(
        repo_root=tmp_path, paths=(Path("coverage.json"),)
    )

    assert violations == []


def test_canonical_json_rejects_non_finite_literals(tmp_path: Path) -> None:
    target = tmp_path / "sample.json"
    target.write_text('{"a": NaN}\n', encoding="utf-8")

    violations = find_noncanonical_json(
        repo_root=tmp_path, paths=(Path("sample.json"),)
    )

    assert len(violations) == 1
    assert "invalid JSON" in violations[0].message


def test_canonical_json_ignores_untracked_files(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    tracked = tmp_path / "tracked.json"
    tracked.write_text('{\n  "a": 1\n}\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.json"], check=True)
    untracked = tmp_path / "scratch.json"
    untracked.write_text('{"z": 0, "a": 1}\n', encoding="utf-8")

    violations = find_noncanonical_json(repo_root=tmp_path, paths=(Path("."),))

    assert violations == []
