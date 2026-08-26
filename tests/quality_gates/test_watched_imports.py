"""Consensus code may only depend on version-digest-watched modules.

The protocol version contract (spec §6, §20.1) hashes WATCHED_PATHS so any
semantic change forces a deliberate version bump. That guarantee is hollow
if a watched module imports constants from an unwatched one — the values
could drift across validators without tripping the digest (exactly how the
payout half-life shipped wrong). This gate closes the loophole: every
``endure.*`` package imported from inside WATCHED_PATHS must itself be
watched, or be on the explicit mechanics allowlist.
"""

from __future__ import annotations

import re
from pathlib import Path

from endure.protocol.version_contract import WATCHED_PATHS

REPO_ROOT = Path(__file__).resolve().parents[2]

# Validator-local mechanics, deliberately outside consensus semantics: storage
# is persistence (what is stored, not how scores are computed).
# Anything else unwatched — especially constant modules — is a violation.
_MECHANICS_ALLOWLIST = {"storage"}

_IMPORT_PATTERN = re.compile(r"^\s*(?:from|import)\s+endure\.([a-z_]+)", re.MULTILINE)


def test_watched_modules_only_import_watched_or_mechanics() -> None:
    watched_packages = {path.name for path in WATCHED_PATHS}
    allowed = watched_packages | _MECHANICS_ALLOWLIST

    violations: list[str] = []
    for watched in WATCHED_PATHS:
        for source in sorted((REPO_ROOT / watched).rglob("*.py")):
            text = source.read_text(encoding="utf-8")
            for package in _IMPORT_PATTERN.findall(text):
                if package not in allowed:
                    violations.append(
                        f"{source.relative_to(REPO_ROOT)} imports "
                        f"endure.{package} (unwatched)"
                    )

    assert not violations, (
        "consensus-critical code imports unwatched modules — move the "
        "imported values inside WATCHED_PATHS or extend the mechanics "
        "allowlist deliberately:\n" + "\n".join(violations)
    )
