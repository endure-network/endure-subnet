"""Every shipped specification must have one indexed public status."""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SPECS = _ROOT / "docs" / "specs"
_INDEX = _ROOT / "docs" / "README.md"
_STATUSES = {"current", "dormant"}
_STATUS_PATTERN = re.compile(r"^> \*\*Status: ([a-z-]+)\.\*\*", re.MULTILINE)


def test_every_spec_has_one_allowed_status_banner() -> None:
    for spec in _SPECS.glob("*.md"):
        statuses = _STATUS_PATTERN.findall(spec.read_text(encoding="utf-8"))
        assert len(statuses) == 1, f"{spec.name} needs exactly one status banner"
        assert statuses[0] in _STATUSES, (
            f"{spec.name} has unknown status {statuses[0]!r}"
        )


def test_documentation_index_classifies_every_tracked_spec_once() -> None:
    index = _INDEX.read_text(encoding="utf-8")
    for spec in _SPECS.glob("*.md"):
        assert index.count(f"specs/{spec.name}") == 1, (
            f"{spec.name} is not classified once"
        )


def test_current_contributor_docs_agree_on_active_vertical() -> None:
    for relative_path in ("README.md", "contrib/CONTRIBUTING.md"):
        text = (_ROOT / relative_path).read_text(encoding="utf-8")
        assert "Alpha Risk V1" in text
        assert "Forge" in text
        assert "dormant" in text
