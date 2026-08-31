from __future__ import annotations

from pathlib import Path

import pytest

import endure.runtime.identity
from endure.runtime.identity import (
    content_revision,
    hash_python_sources,
    runtime_identity,
)


def test_source_run_uses_explicit_local_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ENDURE_SOURCE_REVISION", raising=False)
    monkeypatch.delenv("ENDURE_IMAGE_VERSION", raising=False)

    assert runtime_identity() == {
        "source_revision": "unknown",
        "image_version": "dev",
        "content_revision": content_revision(),
    }


def test_release_image_requires_matching_full_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = "a" * 40
    monkeypatch.setenv("ENDURE_SOURCE_REVISION", revision)
    monkeypatch.setenv("ENDURE_IMAGE_VERSION", f"sha-{revision}")

    assert runtime_identity() == {
        "source_revision": revision,
        "image_version": f"sha-{revision}",
        "content_revision": content_revision(),
    }


def test_content_revision_is_a_stable_sha256_of_the_running_sources() -> None:
    first = content_revision()

    assert len(first) == 64
    assert set(first) <= set("0123456789abcdef")
    assert first == content_revision()


def test_content_revision_matches_an_independent_hash_of_the_package() -> None:
    package_root = Path(endure.runtime.identity.__file__).resolve().parents[1]

    assert content_revision() == hash_python_sources(package_root)


def test_hash_python_sources_ignores_location_and_interpreter_caches(
    tmp_path: Path,
) -> None:
    for parent in (tmp_path / "left", tmp_path / "right"):
        (parent / "pkg").mkdir(parents=True)
        (parent / "pkg" / "mod.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "right" / "pkg" / "__pycache__").mkdir()
    (tmp_path / "right" / "pkg" / "__pycache__" / "mod.py").write_text(
        "value = 2\n", encoding="utf-8"
    )

    assert hash_python_sources(tmp_path / "left") == hash_python_sources(
        tmp_path / "right"
    )


def test_hash_python_sources_refuses_a_root_without_sources(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="no Python sources"):
        hash_python_sources(tmp_path / "absent")


def test_hash_python_sources_changes_when_source_content_changes(
    tmp_path: Path,
) -> None:
    module = tmp_path / "mod.py"
    module.write_text("value = 1\n", encoding="utf-8")
    before = hash_python_sources(tmp_path)
    module.write_text("value = 2\n", encoding="utf-8")

    assert hash_python_sources(tmp_path) != before


@pytest.mark.parametrize(
    ("revision", "image_version"),
    [("unknown", "sha-unknown"), ("a" * 40, "sha-" + "b" * 40)],
)
def test_release_image_rejects_unknown_or_mismatched_revision(
    monkeypatch: pytest.MonkeyPatch, revision: str, image_version: str
) -> None:
    monkeypatch.setenv("ENDURE_SOURCE_REVISION", revision)
    monkeypatch.setenv("ENDURE_IMAGE_VERSION", image_version)

    with pytest.raises(RuntimeError, match="release image"):
        runtime_identity()
