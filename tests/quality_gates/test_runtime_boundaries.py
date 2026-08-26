from __future__ import annotations

from importlib import import_module
from pathlib import Path

import pytest

FORBIDDEN = ("endure.mock", "bittensor_wallet.mock", "config.mock")
TARGETS = (
    Path("endure/base/neuron.py"),
    Path("endure/base/miner.py"),
    Path("endure/base/validator.py"),
)
ENTRYPOINTS = (
    (Path("neurons/miner.py"), "neurons.miner"),
    (Path("neurons/validator.py"), "neurons.validator"),
)


def test_base_runtime_modules_are_mock_free() -> None:
    repo_root = Path(__file__).resolve().parents[2]

    for relative_path in TARGETS:
        contents = (repo_root / relative_path).read_text(encoding="utf-8")
        for forbidden in FORBIDDEN:
            assert forbidden not in contents, (
                f"{relative_path} still contains {forbidden}"
            )


@pytest.mark.parametrize("relative_path, module", ENTRYPOINTS)
def test_neuron_entrypoint_is_endure_owned(relative_path: Path, module: str) -> None:
    repo_root = Path(__file__).resolve().parents[2]

    contents = (repo_root / relative_path).read_text(encoding="utf-8")
    assert "import template" not in contents
    assert "from template" not in contents
    assert import_module(module).__name__ == module
