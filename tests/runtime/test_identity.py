from __future__ import annotations

import pytest

from endure.runtime.identity import runtime_identity


def test_source_run_uses_explicit_local_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ENDURE_SOURCE_REVISION", raising=False)
    monkeypatch.delenv("ENDURE_IMAGE_VERSION", raising=False)

    assert runtime_identity() == {
        "source_revision": "unknown",
        "image_version": "dev",
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
    }


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
