from __future__ import annotations

from importlib import import_module

import pytest


@pytest.mark.parametrize(
    "package",
    (
        "endure.aggregation",
        "endure.api",
        "endure.assessment",
        "endure.assessment.schemas",
        "endure.base",
        "endure.protocol",
        "endure.publication",
        "endure.scoring",
        "endure.scoring.oracle",
        "endure.storage",
        "endure.utils",
    ),
)
def test_endure_package_imports_cleanly(package: str) -> None:
    assert import_module(package).__name__ == package
