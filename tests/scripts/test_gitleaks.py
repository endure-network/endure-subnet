from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from scripts.quality_gates.gitleaks import (
    GITLEAKS_VERSION,
    ReleaseArtifact,
    export_tracked_tree,
    install_archive,
    install_gitleaks,
    release_artifact,
    reported_version,
    require_version,
    scan_tracked_tree,
)


def _stub_gitleaks(path: Path, *, scan_exit: int = 0) -> Path:
    path.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "version" ]; then echo "gitleaks version {GITLEAKS_VERSION}"; '
        "exit 0; fi\n"
        f"exit {scan_exit}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _git_repo_with_config(root: Path) -> Path:
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".gitleaks.toml").write_text(
        "[extend]\nuseDefault = true\n", encoding="utf-8"
    )
    (root / "tracked.txt").write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    return root


@pytest.mark.parametrize(
    ("system", "machine", "suffix"),
    [
        ("Darwin", "arm64", "darwin_arm64.tar.gz"),
        ("Darwin", "x86_64", "darwin_x64.tar.gz"),
        ("Linux", "aarch64", "linux_arm64.tar.gz"),
        ("Linux", "amd64", "linux_x64.tar.gz"),
    ],
)
def test_release_artifact_selection(system: str, machine: str, suffix: str) -> None:
    assert release_artifact(system, machine).filename.endswith(suffix)


def test_release_artifact_rejects_unsupported_platform() -> None:
    with pytest.raises(RuntimeError, match="not pinned"):
        release_artifact("Plan9", "mips")


def test_install_archive_rejects_checksum_mismatch(tmp_path: Path) -> None:
    payload = b"not the release archive"
    artifact = ReleaseArtifact("fixture.tar.gz", hashlib.sha256(b"other").hexdigest())

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        install_archive(payload, artifact=artifact, destination=tmp_path / "gitleaks")


def test_reported_version_enforces_pinned_release() -> None:
    assert reported_version(f"gitleaks version {GITLEAKS_VERSION}") == GITLEAKS_VERSION
    assert reported_version("gitleaks version 9.0.0") != GITLEAKS_VERSION
    with pytest.raises(RuntimeError, match="could not parse"):
        reported_version("development build")


def test_require_version_rejects_another_release(tmp_path: Path) -> None:
    binary = tmp_path / "gitleaks"
    binary.write_text("#!/bin/sh\nprintf '9.0.0\\n'\n", encoding="utf-8")
    binary.chmod(0o755)

    with pytest.raises(RuntimeError, match=f"{GITLEAKS_VERSION} is required"):
        require_version(binary)


def test_export_tracked_tree_uses_working_tree_content(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    export = tmp_path / "export"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    tracked = repo / "tracked.txt"
    tracked.write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    tracked.write_text("modified\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("ignored\n", encoding="utf-8")

    exported = export_tracked_tree(repo, export)

    assert exported == (Path("tracked.txt"),)
    assert (export / "tracked.txt").read_text(encoding="utf-8") == "modified\n"
    assert not (export / "untracked.txt").exists()


def test_export_tracked_tree_preserves_symlinks(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    export = tmp_path / "export"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "real.txt").write_text("real\n", encoding="utf-8")
    (repo / "link.txt").symlink_to("real.txt")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)

    export_tracked_tree(repo, export)

    assert (export / "link.txt").is_symlink()
    assert os.readlink(export / "link.txt") == "real.txt"


def test_install_gitleaks_skips_download_when_pinned_version_present(
    tmp_path: Path,
) -> None:
    destination = _stub_gitleaks(tmp_path / "gitleaks")

    def _forbidden_download(url: str) -> bytes:
        raise AssertionError("must not download when the pinned version is present")

    assert (
        install_gitleaks(destination=destination, downloader=_forbidden_download)
        == destination
    )


def test_scan_tracked_tree_runs_pinned_binary_on_the_tracked_tree(
    tmp_path: Path,
) -> None:
    repo = _git_repo_with_config(tmp_path / "repo")
    scan_tracked_tree(repo, binary=_stub_gitleaks(tmp_path / "gitleaks"))


def test_scan_tracked_tree_propagates_a_scanner_finding(tmp_path: Path) -> None:
    repo = _git_repo_with_config(tmp_path / "repo")
    stub = _stub_gitleaks(tmp_path / "gitleaks", scan_exit=1)
    with pytest.raises(subprocess.CalledProcessError):
        scan_tracked_tree(repo, binary=stub)
