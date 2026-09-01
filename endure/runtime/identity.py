"""Build and source identity surfaced by runtime health checks."""

from __future__ import annotations

import hashlib
import os
import re
from functools import cache
from pathlib import Path

_UNKNOWN_REVISION = "unknown"
_LOCAL_IMAGE_VERSION = "dev"

# The installed `endure` package root. An image copies these sources verbatim,
# so hashing them yields the same digest as hashing `endure/` in a checkout of
# the commit the image was built from.
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def hash_python_sources(root: Path) -> str:
    """Return a stable sha256 over every `*.py` file under `root`.

    Paths are sorted and hashed relative to `root`, so the digest depends only
    on file names and contents, not on the absolute install location. The byte
    separators keep the path/content boundary unambiguous. Interpreter caches
    are excluded because they are build artifacts, not source.

    Raises if `root` holds no sources, so a wrong path cannot be reported as a
    confident digest of nothing.
    """
    digest = hashlib.sha256()
    hashed = 0
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
        hashed += 1
    if hashed == 0:
        raise RuntimeError(f"no Python sources found under {root}")
    return digest.hexdigest()


@cache
def content_revision() -> str:
    """Return the digest of the `endure` sources this process is running.

    Unlike `source_revision`, this is computed from the code itself rather than
    declared by whoever built the image, so it cannot drift from what is
    actually running. Reproduce it from a checkout of the same commit with
    `python -m scripts.content_revision`.
    """
    return hash_python_sources(_PACKAGE_ROOT)


def runtime_identity() -> dict[str, str]:
    """Return source/image identity and reject malformed release-image metadata."""
    source_revision = os.environ.get(
        "ENDURE_SOURCE_REVISION", _UNKNOWN_REVISION
    ).strip()
    image_version = os.environ.get("ENDURE_IMAGE_VERSION", _LOCAL_IMAGE_VERSION).strip()
    if not source_revision:
        source_revision = _UNKNOWN_REVISION
    if not image_version:
        image_version = _LOCAL_IMAGE_VERSION
    if image_version != _LOCAL_IMAGE_VERSION:
        if not re.fullmatch(r"[0-9a-f]{40}", source_revision):
            raise RuntimeError("release image metadata requires a full source revision")
        if image_version != f"sha-{source_revision}":
            raise RuntimeError("release image version must match its source revision")
    return {
        "source_revision": source_revision,
        "image_version": image_version,
        "content_revision": content_revision(),
    }
