from __future__ import annotations

from pathlib import Path

from scripts.quality_gates import checks
from scripts.quality_gates.checks import (
    compute_protocol_digest,
    find_protocol_version_failures,
)


def test_protocol_version_fails_when_digest_drifts(tmp_path: Path) -> None:
    target = tmp_path / "endure" / "protocol" / "__init__.py"
    target.parent.mkdir(parents=True)
    target.write_text('"""Wire protocol (spec §6)."""\n', encoding="utf-8")

    watched = (Path("endure/protocol"),)
    digest = compute_protocol_digest(tmp_path, watched)

    failures = find_protocol_version_failures(
        repo_root=tmp_path,
        watched_paths=watched,
    )

    assert (
        "watched protocol paths changed without updating CURRENT_VERSION_DIGEST"
        in failures
    )
    assert digest


def test_protocol_version_passes_when_contract_state_is_coherent(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "endure" / "protocol" / "__init__.py"
    target.parent.mkdir(parents=True)
    target.write_text('"""Wire protocol (spec §6)."""\n', encoding="utf-8")

    watched = (Path("endure/protocol"),)
    digest = compute_protocol_digest(tmp_path, watched)

    monkeypatch.setattr(checks, "CURRENT_VERSION_DIGEST", digest)
    monkeypatch.setattr(checks, "PREVIOUS_VERSION_DIGEST", digest)
    monkeypatch.setattr(checks, "CURRENT_VERSION_KEY", 11)
    monkeypatch.setattr(checks, "PREVIOUS_VERSION_KEY", 11)

    failures = checks.find_protocol_version_failures(
        repo_root=tmp_path,
        watched_paths=watched,
    )

    assert failures == []


def test_protocol_version_requires_version_bump_when_digest_changes(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "endure" / "protocol" / "__init__.py"
    target.parent.mkdir(parents=True)
    target.write_text('"""Wire protocol (spec §6)."""\n', encoding="utf-8")

    watched = (Path("endure/protocol"),)
    digest = compute_protocol_digest(tmp_path, watched)

    monkeypatch.setattr(checks, "CURRENT_VERSION_DIGEST", digest)
    monkeypatch.setattr(checks, "PREVIOUS_VERSION_DIGEST", "old-digest")
    monkeypatch.setattr(checks, "CURRENT_VERSION_KEY", 10)
    monkeypatch.setattr(checks, "PREVIOUS_VERSION_KEY", 10)

    failures = checks.find_protocol_version_failures(
        repo_root=tmp_path,
        watched_paths=watched,
    )

    assert (
        "CURRENT_VERSION_KEY must increase when CURRENT_VERSION_DIGEST changes"
        in failures
    )


def test_explicit_semantic_file_changes_protocol_digest(tmp_path: Path) -> None:
    repository = tmp_path / "endure" / "storage" / "repository.py"
    repository.parent.mkdir(parents=True)
    repository.write_text("def accepted(): return True\n", encoding="utf-8")
    watched = (Path("endure/storage/repository.py"),)
    before = compute_protocol_digest(tmp_path, watched)

    repository.write_text("def accepted(): return False\n", encoding="utf-8")

    assert compute_protocol_digest(tmp_path, watched) != before
