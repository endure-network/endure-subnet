from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path


def directory_paths(root: Path) -> tuple[Path, ...]:
    """Return every regular file and symlink below root, excluding only .git directories."""
    paths: list[Path] = []
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(directory)
        directory_names[:] = sorted(name for name in directory_names if name != ".git")
        symlink_directories = [
            name for name in directory_names if (current / name).is_symlink()
        ]
        directory_names[:] = [
            name for name in directory_names if name not in symlink_directories
        ]
        candidates = [current / name for name in symlink_directories]
        candidates.extend(current / name for name in sorted(file_names))
        for candidate in candidates:
            mode = candidate.lstat().st_mode
            if stat.S_ISREG(mode) or stat.S_ISLNK(mode):
                paths.append(candidate.relative_to(root))
    return tuple(sorted(paths, key=lambda path: path.as_posix()))


def git_tree_paths(root: Path) -> tuple[Path, ...]:
    """Return precisely the NUL-delimited tracked paths reported by Git."""
    git_executable = shutil.which("git")
    if git_executable is None:
        raise FileNotFoundError("git executable is required for git-tree mode")
    completed = subprocess.run(  # noqa: S603 -- fixed executable and fixed arguments
        [git_executable, "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    raw_paths = completed.stdout.split(b"\0")
    decoded: list[Path] = []
    for raw_path in raw_paths:
        if not raw_path:
            continue
        decoded.append(Path(raw_path.decode("utf-8")))
    return tuple(decoded)
