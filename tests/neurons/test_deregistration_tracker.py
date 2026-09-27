"""Validator wiring of the watched deregistration tracker to storage and resync."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from endure.base.validator import BaseValidatorNeuron
from neurons.validator import Validator


def _validator_with_storage(
    *, persisted: list[str], unfinished: bool = False, registered: list[str]
) -> Validator:
    validator = Validator.__new__(Validator)
    storage = MagicMock()
    storage.assessment_ema_states.return_value = [
        SimpleNamespace(miner_hotkey=hotkey) for hotkey in persisted
    ]
    storage.has_unfinished_assessment_submission.return_value = unfinished
    validator._storage = storage
    validator._schema_id = "risk.v1.subnet_alpha"
    validator.metagraph = SimpleNamespace(hotkeys=registered)
    return validator


def test_startup_seed_confirms_persisted_ema_hotkeys_absent_at_startup() -> None:
    validator = _validator_with_storage(persisted=["hk-gone"], registered=["hk-a"])
    validator._seed_deregistration_tracker()
    tracker = validator._deregistration_tracker()

    tracker.advance({"hk-a"})
    assert tracker.confirmed() == []
    tracker.advance({"hk-a"})
    assert tracker.confirmed() == ["hk-gone"]


@pytest.mark.parametrize(
    ("persisted", "unfinished", "confirmed"),
    [
        ([], False, []),
        ([], True, ["hk-gone"]),
        (["hk-gone"], False, ["hk-gone"]),
    ],
)
def test_prune_forgets_only_fully_archived_hotkeys(
    persisted: list[str], unfinished: bool, confirmed: list[str]
) -> None:
    validator = _validator_with_storage(
        persisted=persisted, unfinished=unfinished, registered=[]
    )
    tracker = validator._deregistration_tracker()
    tracker.advance({"hk-gone"})
    tracker.advance(set())
    tracker.advance(set())

    validator._prune_archived_deregistrations()

    assert tracker.confirmed() == confirmed


def test_resync_advances_tracker_once_per_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator = Validator.__new__(Validator)
    monkeypatch.setattr(BaseValidatorNeuron, "resync_metagraph", lambda self: None)
    validator.metagraph = SimpleNamespace(hotkeys=["hk-a", "hk-b"])
    validator.resync_metagraph()
    validator.metagraph.hotkeys = ["hk-a"]
    validator.resync_metagraph()
    assert validator._deregistration_tracker().confirmed() == []
    validator.resync_metagraph()

    assert validator._deregistration_tracker().confirmed() == ["hk-b"]
