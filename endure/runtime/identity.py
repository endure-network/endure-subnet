"""Build and source identity surfaced by runtime health checks."""

from __future__ import annotations

import os
import re

_UNKNOWN_REVISION = "unknown"
_LOCAL_IMAGE_VERSION = "dev"


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
    }
