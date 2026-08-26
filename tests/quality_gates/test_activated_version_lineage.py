from __future__ import annotations

import hashlib
import importlib
import shutil
import subprocess
from pathlib import Path

import pytest


def _git(repo: Path, *args: str) -> str:
    git = shutil.which("git")
    assert git is not None
    result = subprocess.run(  # noqa: S603 - test controls the argument vector
        [git, *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit_assignment(repo: Path, assignment: tuple[int, str], note: str) -> str:
    key, digest = assignment
    contract = repo / "endure" / "protocol" / "version_contract.py"
    contract.parent.mkdir(parents=True, exist_ok=True)
    contract.write_text(
        f'CURRENT_VERSION_KEY = {key}\nCURRENT_VERSION_DIGEST = "{digest}"\n# {note}\n',
        encoding="utf-8",
    )
    _git(repo, "add", str(contract.relative_to(repo)))
    _git(
        repo,
        "-c",
        "user.name=Endure Test",
        "-c",
        "user.email=endure-test.invalid",
        "commit",
        "-m",
        note,
    )
    return _git(repo, "rev-parse", "HEAD")


def test_first_parent_lineage_reports_distinct_assignments_and_receipts(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "staging")
    first_digest = "a" * 64
    second_digest = "b" * 64
    first_commit = _commit_assignment(repo, (10, first_digest), "first")
    _commit_assignment(repo, (10, first_digest), "same assignment")
    _git(repo, "checkout", "-b", "feature")
    topic_commit = _commit_assignment(repo, (11, second_digest), "second")
    _git(repo, "checkout", "staging")
    _git(
        repo,
        "-c",
        "user.name=Endure Test",
        "-c",
        "user.email=endure-test.invalid",
        "merge",
        "--no-ff",
        "feature",
        "-m",
        "merge second",
    )
    merge_commit = _git(repo, "rev-parse", "HEAD")
    lineage = importlib.import_module("scripts.quality_gates.activated_version_lineage")

    activations = lineage.read_first_parent_activations(repo, "staging")

    assert not isinstance(activations, str)
    assert tuple(
        (
            activation.source_commit,
            activation.key,
            activation.digest,
            activation.evidence_sha256,
        )
        for activation in activations
    ) == (
        (
            first_commit,
            10,
            first_digest,
            hashlib.sha256(
                (
                    f"SOURCE_COMMIT_SHA1={first_commit}\n"
                    f"CURRENT_VERSION_KEY=10\n"
                    f"CURRENT_VERSION_DIGEST={first_digest}\n"
                ).encode()
            ).hexdigest(),
        ),
        (
            merge_commit,
            11,
            second_digest,
            hashlib.sha256(
                (
                    f"SOURCE_COMMIT_SHA1={merge_commit}\n"
                    f"CURRENT_VERSION_KEY=11\n"
                    f"CURRENT_VERSION_DIGEST={second_digest}\n"
                ).encode()
            ).hexdigest(),
        ),
    )
    assert topic_commit != merge_commit


@pytest.mark.parametrize("lineage_ref", ["--", "staging..HEAD", "staging~1"])
def test_lineage_rejects_revision_expressions(tmp_path: Path, lineage_ref: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "staging")
    _commit_assignment(repo, (10, "a" * 64), "first")
    lineage = importlib.import_module("scripts.quality_gates.activated_version_lineage")

    result = lineage.read_first_parent_activations(repo, lineage_ref)

    assert result == "first-parent staging lineage cannot be read: invalid commit ref"
