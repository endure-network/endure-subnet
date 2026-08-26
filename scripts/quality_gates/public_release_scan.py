from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Final, Sequence

from scripts.quality_gates.public_release_scan_models import (
    ScanConfig,
    ScanError,
    ScanReport,
)
from scripts.quality_gates.public_release_scan_paths import (
    directory_paths,
    git_tree_paths,
)

PRIVATE_MANIFEST_PATH: Final[Path] = (
    Path.home() / ".config/endure/public-release-denylist.toml"
)
ALLOWLIST_RELATIVE_PATH: Final[Path] = Path(
    "scripts/quality_gates/public_release_allowlist.toml"
)


def scan_directory(config: ScanConfig) -> ScanReport:
    """Scan every regular file and symlink below the configured directory root."""
    from scripts.quality_gates.public_release_scan_engine import scan_paths

    return scan_paths(config, directory_paths(config.root))


def _scan_git_tree(root: Path) -> ScanReport:
    from scripts.quality_gates.public_release_scan_engine import scan_paths

    config = ScanConfig(
        root=root,
        allowlist_path=root / ALLOWLIST_RELATIVE_PATH,
        denylist_path=PRIVATE_MANIFEST_PATH,
    )
    return scan_paths(config, git_tree_paths(root))


def _directory_config(root: Path) -> ScanConfig:
    return ScanConfig(
        root=root,
        allowlist_path=root / ALLOWLIST_RELATIVE_PATH,
        denylist_path=PRIVATE_MANIFEST_PATH,
    )


def _emit(report: ScanReport) -> int:
    print(json.dumps(report.to_payload(), indent=2, sort_keys=True))
    return 1 if report.failed else 0


def _error_report(message: str) -> ScanReport:
    return ScanReport((), (ScanError("scan-error", message),), 0, 0)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one explicit scanner mode and emit its deterministic redacted report."""
    parser = argparse.ArgumentParser(
        description="Scan a candidate public release tree."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--git-tree", action="store_true")
    mode.add_argument("--directory", type=Path, metavar="PATH")
    arguments = parser.parse_args(argv)
    if arguments.git_tree:
        try:
            return _emit(_scan_git_tree(Path.cwd()))
        except (OSError, subprocess.CalledProcessError, UnicodeDecodeError):
            return _emit(_error_report("could not enumerate UTF-8 git tree paths"))
    directory = arguments.directory.resolve()
    if not directory.is_dir():
        return _emit(_error_report("directory mode requires an existing directory"))
    return _emit(scan_directory(_directory_config(directory)))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
