"""The served Alpha runtime must not load the dormant vertical's machinery.

Forge lending stays in the tree as a production-gated future vertical. Behavioral
isolation means the live neuron and read-API entrypoints do not drag its scoring
machinery into a default Alpha process at import time. Selecting Forge via
``--endure.active_schema`` still works; it just pays for those imports on its own
branch.

Each import runs in a clean subprocess: pytest collection has already imported
most of the tree in-process, so ``sys.modules`` here proves nothing.

Two deliberate non-goals. ``endure.assessment.schemas.forge_lending`` is not
forbidden anywhere: ``LendingSubmissionBundle`` is a registry-level protocol type
that every entrypoint loads by design. Nor is
``endure.assessment.lending_universe``, whose provider the registry constructs at
module scope. Only the dormant vertical's *implementation* is forbidden.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

FORBIDDEN = ("endure.scoring.lending", "endure.protocol.lending_miner")

ISOLATED_ENTRYPOINTS = ("neurons.validator", "neurons.miner", "endure.api.app")


def _modules_loaded_by(module: str) -> set[str]:
    probe = f"import json, sys;import {module};print(json.dumps(sorted(sys.modules)))"
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"importing {module} failed:\n{result.stderr}"
    return set(json.loads(result.stdout))


@pytest.mark.parametrize("module", ISOLATED_ENTRYPOINTS)
def test_default_runtime_excludes_the_dormant_vertical(module: str) -> None:
    loaded = _modules_loaded_by(module)

    leaked = sorted(
        name
        for name in loaded
        for prefix in FORBIDDEN
        if name == prefix or name.startswith(f"{prefix}.")
    )

    assert not leaked, (
        f"importing {module} loaded dormant-vertical modules {leaked} — keep "
        "those imports function-local to the branch that needs them"
    )
