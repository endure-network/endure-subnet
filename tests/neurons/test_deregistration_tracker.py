from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from endure.base.validator import BaseValidatorNeuron
from neurons.validator import Validator


def _bare_validator() -> Validator:
    return Validator.__new__(Validator)


class TestDeregistrationTracker:
    def test_confirms_only_after_two_consecutive_missing_generations(self) -> None:
        validator = _bare_validator()
        validator._advance_deregistration_tracker({"hk-a", "hk-b"})
        assert validator._confirmed_deregistered() == []

        validator._advance_deregistration_tracker({"hk-a"})
        assert validator._confirmed_deregistered() == []

        validator._advance_deregistration_tracker({"hk-a"})
        assert validator._confirmed_deregistered() == ["hk-b"]

    def test_reappearance_resets_the_missing_count(self) -> None:
        validator = _bare_validator()
        validator._advance_deregistration_tracker({"hk-a", "hk-b"})
        validator._advance_deregistration_tracker({"hk-a"})
        validator._advance_deregistration_tracker({"hk-a", "hk-b"})
        validator._advance_deregistration_tracker({"hk-a"})
        assert validator._confirmed_deregistered() == []

    def test_scoring_passes_between_syncs_never_confirm(self) -> None:
        validator = _bare_validator()
        validator._advance_deregistration_tracker({"hk-a", "hk-b"})
        validator._advance_deregistration_tracker({"hk-a"})
        for _ in range(10):
            assert validator._confirmed_deregistered() == []
        validator._advance_deregistration_tracker({"hk-a"})
        assert validator._confirmed_deregistered() == ["hk-b"]

    def test_hotkey_never_seen_registered_is_not_tracked(self) -> None:
        validator = _bare_validator()
        validator._advance_deregistration_tracker({"hk-a"})
        validator._advance_deregistration_tracker({"hk-a"})
        assert validator._confirmed_deregistered() == []

    def test_multiple_hotkeys_confirm_sorted(self) -> None:
        validator = _bare_validator()
        validator._advance_deregistration_tracker({"hk-c", "hk-a", "hk-b"})
        validator._advance_deregistration_tracker(set())
        validator._advance_deregistration_tracker(set())
        assert validator._confirmed_deregistered() == ["hk-a", "hk-b", "hk-c"]

    def test_confirmed_hotkey_is_pruned_after_ema_state_is_archived(self) -> None:
        validator = _bare_validator()
        storage = MagicMock()
        storage.assessment_ema_states.return_value = []
        storage.has_unfinished_assessment_submission.return_value = False
        validator._storage = storage
        validator._schema_id = "risk.v1.subnet_alpha"
        validator._advance_deregistration_tracker({"hk-gone"})
        validator._advance_deregistration_tracker(set())
        validator._advance_deregistration_tracker(set())

        validator._prune_archived_deregistrations()
        assert validator._confirmed_deregistered() == []

    def test_confirmed_hotkey_with_unresolved_submission_is_not_pruned(self) -> None:
        validator = _bare_validator()
        storage = MagicMock()
        storage.assessment_ema_states.return_value = []
        storage.has_unfinished_assessment_submission.return_value = True
        validator._storage = storage
        validator._schema_id = "risk.v1.subnet_alpha"
        validator._advance_deregistration_tracker({"hk-gone"})
        validator._advance_deregistration_tracker(set())
        validator._advance_deregistration_tracker(set())

        validator._prune_archived_deregistrations()

        assert validator._confirmed_deregistered() == ["hk-gone"]


class TestDeregistrationTrackerSeeding:
    def test_seed_tracks_persisted_ema_hotkeys_absent_at_startup(self) -> None:
        validator = _bare_validator()
        storage = MagicMock()
        storage.assessment_ema_states.return_value = [
            SimpleNamespace(miner_hotkey="hk-gone")
        ]
        validator._storage = storage
        validator._schema_id = "risk.v1.subnet_alpha"
        metagraph = MagicMock()
        metagraph.hotkeys = ["hk-a"]
        validator.metagraph = metagraph

        validator._seed_deregistration_tracker()
        storage.assessment_ema_states.assert_called_once_with("risk.v1.subnet_alpha")

        validator._advance_deregistration_tracker({"hk-a"})
        assert validator._confirmed_deregistered() == []
        validator._advance_deregistration_tracker({"hk-a"})
        assert validator._confirmed_deregistered() == ["hk-gone"]

    def test_seed_keeps_registered_hotkeys_untracked(self) -> None:
        validator = _bare_validator()
        storage = MagicMock()
        storage.assessment_ema_states.return_value = [
            SimpleNamespace(miner_hotkey="hk-a")
        ]
        validator._storage = storage
        validator._schema_id = "risk.v1.subnet_alpha"
        metagraph = MagicMock()
        metagraph.hotkeys = ["hk-a"]
        validator.metagraph = metagraph

        validator._seed_deregistration_tracker()
        validator._advance_deregistration_tracker({"hk-a"})
        validator._advance_deregistration_tracker({"hk-a"})
        assert validator._confirmed_deregistered() == []


class TestResyncMetagraphSeam:
    def test_resync_override_advances_tracker_per_generation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        validator = _bare_validator()
        monkeypatch.setattr(BaseValidatorNeuron, "resync_metagraph", lambda self: None)
        metagraph = MagicMock()
        validator.metagraph = metagraph

        metagraph.hotkeys = ["hk-a", "hk-b"]
        validator.resync_metagraph()
        metagraph.hotkeys = ["hk-a"]
        validator.resync_metagraph()
        assert validator._confirmed_deregistered() == []
        validator.resync_metagraph()
        assert validator._confirmed_deregistered() == ["hk-b"]
