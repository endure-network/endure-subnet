"""Install and run the repository-pinned Gitleaks release."""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Final, Sequence

GITLEAKS_VERSION: Final = "8.30.1"
RELEASE_BASE_URL: Final = (
    f"https://github.com/gitleaks/gitleaks/releases/download/v{GITLEAKS_VERSION}"
)
VERSION_PATTERN: Final = re.compile(r"(?<!\d)v?(\d+\.\d+\.\d+)(?!\d)")


@dataclass(frozen=True, slots=True)
class ReleaseArtifact:
    filename: str
    sha256: str


ARTIFACTS: Final[dict[tuple[str, str], ReleaseArtifact]] = {
    ("darwin", "arm64"): ReleaseArtifact(
        f"gitleaks_{GITLEAKS_VERSION}_darwin_arm64.tar.gz",
        "b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5",
    ),
    ("darwin", "x64"): ReleaseArtifact(
        f"gitleaks_{GITLEAKS_VERSION}_darwin_x64.tar.gz",
        "dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709",
    ),
    ("linux", "arm64"): ReleaseArtifact(
        f"gitleaks_{GITLEAKS_VERSION}_linux_arm64.tar.gz",
        "e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080",
    ),
    ("linux", "x64"): ReleaseArtifact(
        f"gitleaks_{GITLEAKS_VERSION}_linux_x64.tar.gz",
        "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
    ),
}


def release_artifact(system: str, machine: str) -> ReleaseArtifact:
    """Resolve the pinned release asset for a supported host platform."""
    normalized_system = system.lower()
    normalized_machine = machine.lower()
    architectures = {
        "arm64": "arm64",
        "aarch64": "arm64",
        "x86_64": "x64",
        "amd64": "x64",
    }
    architecture = architectures.get(normalized_machine, normalized_machine)
    try:
        return ARTIFACTS[(normalized_system, architecture)]
    except KeyError as exc:
        raise RuntimeError(
            f"Gitleaks {GITLEAKS_VERSION} is not pinned for {system}/{machine}"
        ) from exc


def default_binary_path() -> Path:
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache_home / "endure" / f"gitleaks-{GITLEAKS_VERSION}" / "gitleaks"


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
        return response.read()


def install_archive(
    archive: bytes, *, artifact: ReleaseArtifact, destination: Path
) -> None:
    """Verify a release archive and install only its Gitleaks executable."""
    actual_digest = hashlib.sha256(archive).hexdigest()
    if actual_digest != artifact.sha256:
        raise RuntimeError(
            f"checksum mismatch for {artifact.filename}: expected "
            f"{artifact.sha256}, got {actual_digest}"
        )

    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        candidates = [
            member
            for member in bundle.getmembers()
            if member.isfile() and Path(member.name).name == "gitleaks"
        ]
        if len(candidates) != 1:
            raise RuntimeError(
                f"{artifact.filename} must contain exactly one gitleaks executable"
            )
        extracted = bundle.extractfile(candidates[0])
        if extracted is None:
            raise RuntimeError(f"could not read gitleaks from {artifact.filename}")
        executable = extracted.read()

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
        temporary_path = Path(handle.name)
        handle.write(executable)
    temporary_path.chmod(0o755)
    temporary_path.replace(destination)


def install_gitleaks(
    destination: Path | None = None,
    *,
    downloader: Callable[[str], bytes] = _download,
) -> Path:
    destination = destination or default_binary_path()
    try:
        require_version(destination)
    except (OSError, RuntimeError, subprocess.CalledProcessError):
        pass
    else:
        return destination
    artifact = release_artifact(platform.system(), platform.machine())
    archive = downloader(f"{RELEASE_BASE_URL}/{artifact.filename}")
    install_archive(archive, artifact=artifact, destination=destination)
    require_version(destination)
    return destination


def reported_version(output: str) -> str:
    match = VERSION_PATTERN.search(output)
    if match is None:
        raise RuntimeError(f"could not parse Gitleaks version from {output!r}")
    return match.group(1)


def require_version(binary: Path) -> None:
    if not binary.is_file():
        raise RuntimeError(
            f"Gitleaks {GITLEAKS_VERSION} is not installed; run `make bootstrap`"
        )
    result = subprocess.run(  # noqa: S603
        [str(binary), "version"],
        check=True,
        capture_output=True,
        text=True,
    )
    actual = reported_version(f"{result.stdout}\n{result.stderr}")
    if actual != GITLEAKS_VERSION:
        raise RuntimeError(
            f"Gitleaks {GITLEAKS_VERSION} is required; found {actual} at {binary}"
        )


def tracked_paths(repo_root: Path) -> tuple[Path, ...]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required to enumerate the tracked tree")
    result = subprocess.run(  # noqa: S603
        [git, "-C", str(repo_root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return tuple(
        Path(os.fsdecode(entry)) for entry in result.stdout.split(b"\0") if entry
    )


def export_tracked_tree(repo_root: Path, destination: Path) -> tuple[Path, ...]:
    """Copy the current contents of tracked files into an isolated tree."""
    exported: list[Path] = []
    for relative_path in tracked_paths(repo_root):
        source = repo_root / relative_path
        if not source.exists() and not source.is_symlink():
            continue
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(os.readlink(source))
        else:
            shutil.copy2(source, target)
        exported.append(relative_path)
    return tuple(exported)


def scan_tracked_tree(repo_root: Path, binary: Path | None = None) -> None:
    binary = binary or default_binary_path()
    require_version(binary)
    config_path = repo_root / ".gitleaks.toml"
    if not config_path.is_file():
        raise RuntimeError(f"missing Gitleaks configuration: {config_path}")

    with tempfile.TemporaryDirectory(prefix="endure-gitleaks-") as temp_dir:
        export_root = Path(temp_dir)
        exported = export_tracked_tree(repo_root, export_root)
        subprocess.run(  # noqa: S603
            [
                str(binary),
                "dir",
                str(export_root),
                "--config",
                str(config_path),
                "--redact",
                "--no-banner",
            ],
            check=True,
        )
    print(f"Gitleaks scan passed ({len(exported)} tracked paths).")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "scan"))
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "install":
            installed = install_gitleaks()
            print(f"Installed Gitleaks {GITLEAKS_VERSION} at {installed}")
        else:
            scan_tracked_tree(Path.cwd())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Gitleaks {arguments.command} failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
