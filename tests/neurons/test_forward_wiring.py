from __future__ import annotations

import asyncio
import threading
from decimal import Decimal
from unittest.mock import MagicMock

from neurons.validator import Validator


def _wired_validator(service: MagicMock) -> Validator:
    validator = Validator.__new__(Validator)
    validator._service = service
    validator._blended_snapshot = {}
    validator._dereg_missing_counts = {"hk-gone": 2}
    validator._dereg_last_registered = {"hk-a"}
    metagraph = MagicMock()
    metagraph.hotkeys = ["hk-a"]
    validator.metagraph = metagraph
    validator._tick_failures = 0
    validator._last_tick_ok = None
    validator._last_tick_error = None
    validator._shutdown_event = threading.Event()
    config = MagicMock()
    config.endure.tick_seconds = 0
    validator.config = config
    return validator


class TestForwardWiring:
    def test_forward_threads_archive_hotkeys_and_caches_snapshot(self) -> None:
        service = MagicMock()
        service.tick.return_value = {"hk-a": Decimal("1")}
        service.blended_snapshot.return_value = {"hk-a": Decimal("0.5")}
        validator = _wired_validator(service)

        asyncio.run(validator.forward())

        service.tick.assert_called_once_with(
            expected_miners=["hk-a"], archive_hotkeys=["hk-gone"]
        )
        assert validator._blended_snapshot == {"hk-a": Decimal("0.5")}
        assert validator.scores == [Decimal("1")]
        assert validator._tick_failures == 0

    def test_forward_retains_snapshot_when_tick_scores_nothing(self) -> None:
        service = MagicMock()
        service.tick.return_value = {"hk-a": Decimal("1")}
        service.blended_snapshot.return_value = {"hk-a": Decimal("0.5")}
        validator = _wired_validator(service)
        asyncio.run(validator.forward())

        service.tick.return_value = None
        asyncio.run(validator.forward())

        assert validator._blended_snapshot == {"hk-a": Decimal("0.5")}
        assert service.blended_snapshot.call_count == 1

    def test_forward_applies_weights_before_prune_failure(self) -> None:
        # Given: a completed scoring tick and a pruning failure.
        service = MagicMock()
        service.tick.return_value = {"hk-a": Decimal("1")}
        service.blended_snapshot.return_value = {"hk-a": Decimal("0.5")}
        validator = _wired_validator(service)
        validator._prune_archived_deregistrations = MagicMock(
            side_effect=RuntimeError("prune failed")
        )

        # When: the validator completes its forward pass.
        asyncio.run(validator.forward())

        # Then: the computed weight survives the independent pruning failure.
        assert validator.scores == [Decimal("1")]

    def test_forward_retains_confirmed_hotkey_with_unresolved_submission(self) -> None:
        service = MagicMock()
        service.tick.return_value = None
        validator = _wired_validator(service)
        storage = MagicMock()
        storage.assessment_ema_states.return_value = []
        storage.has_unfinished_assessment_submission.return_value = True
        validator._storage = storage
        validator._schema_id = "risk.v1.subnet_alpha"

        asyncio.run(validator.forward())

        assert validator._confirmed_deregistered() == ["hk-gone"]
