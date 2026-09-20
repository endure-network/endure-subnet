"""Validate the administrator-controlled files used by the system timer."""

import os
import stat
import sys
from pathlib import Path


def validate_path(path: Path) -> None:
    """Reject replaceable files, links, and ancestor directories."""
    path = path.absolute()
    for component in (path, *path.parents):
        metadata = component.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_mode & 0o022
        ):
            raise ValueError(f"Unsafe installation path: {component}")


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("Installation validation requires root.")
    try:
        for argument in sys.argv[1:]:
            validate_path(Path(argument))
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
