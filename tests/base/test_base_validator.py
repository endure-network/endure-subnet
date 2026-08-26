"""Tests for endure.base.validator.BaseValidatorNeuron.

Constructor, update_scores, resync_metagraph, context manager exit, and
sandboxed state persistence. Disk-backed save_state/load_state paths include
round-trip and corruption-quarantine coverage.

Note: `from __future__ import annotations` is deliberately NOT used
(see test_base_miner for the bittensor 10.x Axon.attach runtime
introspection reason).
"""

import logging
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import bittensor as bt
import numpy as np
import pytest
from bittensor.utils.mock.subtensor_mock import __GLOBAL_MOCK_STATE__

from endure.base.rate_gate import AdaptiveRpcGate, GatedSubtensor, RateLimited
from endure.base.validator import BaseValidatorNeuron
from endure.runtime.mock import MockRuntimeProvider, MockSubtensor

pytestmark = pytest.mark.filterwarnings(
    "ignore::pydantic.warnings.PydanticDeprecatedSince20"
)


class _ConcreteValidator(BaseValidatorNeuron):
    """Minimal BaseValidatorNeuron with a no-op forward coroutine."""

    async def forward(self) -> None:
        return None

    def run(self) -> None:
        return None


class _FailingRuntimeValidator(BaseValidatorNeuron):
    """Uses the inherited BaseValidatorNeuron.run() to exercise runtime logging."""

    async def forward(self) -> None:
        raise RuntimeError("boom")


@pytest.fixture
def validator_config(mock_validator_config: bt.Config) -> bt.Config:
    # Disable axon.serve + weight-setting to keep the constructor
    # off the chain surface BaseValidatorNeuron would normally touch.
    mock_validator_config.neuron.axon_off = True
    mock_validator_config.neuron.disable_set_weights = True
    return mock_validator_config


@pytest.fixture
def validator(
    validator_config: bt.Config,
    mock_runtime_provider: MockRuntimeProvider,
    trap_external_ip: dict[str, int],
):
    assert trap_external_ip["count"] == 0
    return _ConcreteValidator(
        config=validator_config,
        runtime_provider=mock_runtime_provider,
    )


class TestConstructor:
    def test_wires_dendrite_and_scores(
        self,
        validator: _ConcreteValidator,
        trap_external_ip: dict[str, int],
    ) -> None:
        assert validator.dendrite is not None
        assert trap_external_ip["count"] == 0
        assert validator.neuron_type == "ValidatorNeuron"
        assert isinstance(validator.scores, list)
        assert validator.scores == [Decimal("0")] * int(validator.metagraph.n)

    def test_inherits_base_neuron_wiring(self, validator: _ConcreteValidator) -> None:
        assert validator.wallet is not None
        assert validator.subtensor is not None
        assert validator.metagraph is not None

    def test_hotkeys_snapshot_matches_metagraph(
        self, validator: _ConcreteValidator
    ) -> None:
        assert validator.hotkeys == list(validator.metagraph.hotkeys)

    def test_load_state_missing_file_is_safe(
        self, validator: _ConcreteValidator
    ) -> None:
        state_path = Path(validator.config.neuron.full_path) / "state.npz"
        if state_path.exists():
            state_path.unlink()

        before_scores = validator.scores.copy()
        before_step = validator.step
        before_hotkeys = list(validator.hotkeys)

        validator.load_state()

        assert validator.step == before_step
        assert validator.scores == before_scores
        assert validator.hotkeys == before_hotkeys

    def test_raises_when_axon_creation_fails(
        self,
        validator_config: bt.Config,
        mock_runtime_provider: MockRuntimeProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        validator_config.neuron.axon_off = False
        failure = RuntimeError("axon creation failed")
        monkeypatch.setattr(
            mock_runtime_provider,
            "create_validator_axon",
            MagicMock(side_effect=failure),
        )

        with pytest.raises(RuntimeError, match="axon creation failed"):
            _ConcreteValidator(
                config=validator_config,
                runtime_provider=mock_runtime_provider,
            )

    def test_raises_when_axon_publication_is_unsuccessful(
        self,
        validator_config: bt.Config,
        mock_runtime_provider: MockRuntimeProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        validator_config.neuron.axon_off = False
        unpublished = MagicMock()
        unpublished.success = False
        unpublished.message = "registration rejected"
        monkeypatch.setattr(
            MockSubtensor,
            "serve_axon",
            MagicMock(return_value=unpublished),
        )

        with pytest.raises(RuntimeError, match="registration rejected"):
            _ConcreteValidator(
                config=validator_config,
                runtime_provider=mock_runtime_provider,
            )


class TestLoadStateCorruption:
    def test_corrupt_state_file_falls_back_and_quarantines(
        self, validator: _ConcreteValidator
    ) -> None:
        state_path = Path(validator.config.neuron.full_path) / "state.npz"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_bytes(b"not a real npz archive")
        before_scores = validator.scores.copy()

        validator.load_state()  # must not raise

        assert validator.scores == before_scores
        assert not state_path.exists()
        assert list(state_path.parent.glob("state.npz.corrupt*"))


class TestStateRoundTrip:
    def test_state_round_trip_restores_persisted_fields_exactly(
        self,
        validator: _ConcreteValidator,
        mock_runtime_provider: MockRuntimeProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        expected_step = 37
        transient_scores = [
            Decimal("0.125"),
            Decimal("2.375"),
            Decimal("-0.0625"),
        ]
        expected_hotkeys = ["alpha-hotkey", "beta-hotkey", "gamma-hotkey"]
        validator.step = expected_step
        validator.scores = transient_scores.copy()
        validator.hotkeys = expected_hotkeys.copy()
        validator.save_state()
        state_path = Path(validator.config.neuron.full_path) / "state.npz"
        with np.load(state_path) as state:
            assert set(state.files) == {"step", "hotkeys"}
        __GLOBAL_MOCK_STATE__.clear()
        load_state = _ConcreteValidator.load_state
        monkeypatch.setattr(_ConcreteValidator, "load_state", lambda _self: None)
        monkeypatch.setattr(_ConcreteValidator, "sync", lambda _self: None)

        restored = _ConcreteValidator(
            config=validator.config,
            runtime_provider=mock_runtime_provider,
        )
        load_state(restored)

        assert restored.step == expected_step
        assert restored.scores == [Decimal("0")] * int(restored.metagraph.n)
        assert restored.hotkeys == expected_hotkeys

        legacy_scores = transient_scores.copy()
        fallback_scores = [Decimal("0.75"), Decimal("1.25")]
        state_path = Path(validator.config.neuron.full_path) / "state.npz"
        np.savez(
            state_path,
            step=expected_step,
            scores=np.array([str(score) for score in legacy_scores]),
            hotkeys=np.array(expected_hotkeys),
        )
        restored.scores = fallback_scores.copy()

        load_state(restored)

        assert restored.step == expected_step
        assert restored.hotkeys == expected_hotkeys
        assert restored.scores == fallback_scores
        assert state_path.exists()
        assert not list(state_path.parent.glob("state.npz.corrupt*"))

    def test_garbage_legacy_scores_still_restores_step_and_hotkeys(
        self,
        validator: _ConcreteValidator,
        mock_runtime_provider: MockRuntimeProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        expected_step = 41
        expected_hotkeys = ["alpha-hotkey", "beta-hotkey"]
        state_path = Path(validator.config.neuron.full_path) / "state.npz"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(
            state_path,
            step=expected_step,
            scores=np.array(["not-a-decimal", "NaN", "@@@"]),
            hotkeys=np.array(expected_hotkeys),
        )
        __GLOBAL_MOCK_STATE__.clear()
        load_state = _ConcreteValidator.load_state
        monkeypatch.setattr(_ConcreteValidator, "load_state", lambda _self: None)
        monkeypatch.setattr(_ConcreteValidator, "sync", lambda _self: None)
        restored = _ConcreteValidator(
            config=validator.config,
            runtime_provider=mock_runtime_provider,
        )

        load_state(restored)

        assert restored.step == expected_step
        assert restored.hotkeys == expected_hotkeys
        assert restored.scores == [Decimal("0")] * int(restored.metagraph.n)
        assert state_path.exists()
        assert not list(state_path.parent.glob("state.npz.corrupt*"))

    def test_state_round_trip_corruption_falls_back_and_quarantines(
        self, validator: _ConcreteValidator
    ) -> None:
        validator.step = 37
        validator.scores = [Decimal("0.125"), Decimal("2.375")]
        validator.hotkeys = ["alpha-hotkey", "beta-hotkey"]
        validator.save_state()
        state_path = Path(validator.config.neuron.full_path) / "state.npz"
        state_path.write_bytes(b"corrupted after save")
        fallback_step = 11
        fallback_scores = [Decimal("0.75"), Decimal("1.25")]
        fallback_hotkeys = ["fallback-alpha", "fallback-beta"]
        validator.step = fallback_step
        validator.scores = fallback_scores.copy()
        validator.hotkeys = fallback_hotkeys.copy()

        validator.load_state()

        assert validator.step == fallback_step
        assert validator.scores == fallback_scores
        assert validator.hotkeys == fallback_hotkeys
        assert not state_path.exists()
        assert list(state_path.parent.glob("state.npz.corrupt*"))


class TestWeightNormalization:
    def test_negative_scores_are_clamped_not_inverted(
        self, validator: _ConcreteValidator
    ) -> None:
        validator.scores = [Decimal("-2"), Decimal("1")]
        assert validator._normalized_weights() == [Decimal("0"), Decimal("1")]

    def test_all_nonpositive_scores_yield_abstain_vector(
        self, validator: _ConcreteValidator
    ) -> None:
        validator.scores = [Decimal("0"), Decimal("-5")]
        assert validator._normalized_weights() == [Decimal("0"), Decimal("0")]

    def test_positive_scores_normalize_to_one(
        self, validator: _ConcreteValidator
    ) -> None:
        validator.scores = [Decimal("1"), Decimal("3")]
        weights = validator._normalized_weights()
        assert weights == [Decimal("0.25"), Decimal("0.75")]
        assert sum(weights, Decimal("0")) == Decimal("1")

    def test_set_weights_abstains_when_no_positive_scores(
        self, validator: _ConcreteValidator
    ) -> None:
        validator.step = 5
        validator.scores = [Decimal("0")] * int(validator.metagraph.n)
        spy = MagicMock()
        validator.subtensor.set_weights = spy

        validator.set_weights()

        spy.assert_not_called()


class TestUpdateScores:
    def test_applies_moving_average_for_single_uid(
        self, validator: _ConcreteValidator
    ) -> None:
        alpha = Decimal(str(validator.config.neuron.moving_average_alpha))
        n = int(validator.metagraph.n)
        validator.scores = [Decimal("0")] * n
        rewards = [Decimal("1.0")]
        validator.update_scores(rewards=rewards, uids=[0])
        # Expected: scores[0] = alpha * 1.0 + (1-alpha) * 0 == alpha.
        assert validator.scores[0] == alpha
        # Other indices must remain zero.
        assert all(score == Decimal("0") for score in validator.scores[1:])

    def test_nan_rewards_are_sanitized_and_logged(
        self,
        validator: _ConcreteValidator,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        n = int(validator.metagraph.n)
        validator.scores = [Decimal("0")] * n
        rewards = np.array([np.nan], dtype=np.float32)
        with caplog.at_level(logging.WARNING, logger="bittensor"):
            validator.update_scores(rewards=rewards, uids=[0])
        assert validator.scores[0] == Decimal("0")

    @pytest.mark.parametrize("reward", [Decimal("Infinity"), Decimal("-Infinity")])
    def test_non_finite_rewards_are_sanitized_before_normalization(
        self, validator: _ConcreteValidator, reward: Decimal
    ) -> None:
        validator.scores = [Decimal("0")] * int(validator.metagraph.n)

        validator.update_scores(rewards=[reward], uids=[0])

        assert all(score.is_finite() for score in validator.scores)
        assert validator._normalized_weights() == [Decimal("0")] * len(validator.scores)

    def test_empty_uids_is_noop(self, validator: _ConcreteValidator) -> None:
        before = validator.scores.copy()
        validator.update_scores(rewards=np.array([]), uids=[])
        assert before == validator.scores

    def test_length_mismatch_raises_value_error(
        self, validator: _ConcreteValidator
    ) -> None:
        with pytest.raises(ValueError, match="Shape mismatch"):
            validator.update_scores(rewards=np.array([0.1, 0.2]), uids=[0])

    def test_accepts_numpy_uids_without_copy_warning(
        self, validator: _ConcreteValidator
    ) -> None:
        alpha = Decimal(str(validator.config.neuron.moving_average_alpha))
        validator.update_scores(
            rewards=[Decimal("0.5")],
            uids=np.array([0], dtype=np.int64),
        )
        assert validator.scores[0] == alpha * Decimal("0.5")

    def test_out_of_range_uid_is_skipped_without_indexerror(
        self, validator: _ConcreteValidator
    ) -> None:
        n = int(validator.metagraph.n)
        validator.scores = [Decimal("0")] * n
        # uid == n is one past the last valid index; the old code raised
        # IndexError here. It must now be skipped, leaving scores untouched.
        validator.update_scores(rewards=[Decimal("1.0")], uids=[n])
        assert all(score == Decimal("0") for score in validator.scores)

    def test_negative_uid_is_skipped(self, validator: _ConcreteValidator) -> None:
        n = int(validator.metagraph.n)
        validator.scores = [Decimal("0")] * n
        validator.update_scores(rewards=[Decimal("1.0")], uids=[-1])
        assert all(score == Decimal("0") for score in validator.scores)


class TestResyncMetagraph:
    def test_resizes_scores_to_metagraph_n(self, validator: _ConcreteValidator) -> None:
        # Shrink the scores array so resync_metagraph's grow-path
        # (len(self.scores) != metagraph.n) must expand it.
        validator.scores = [Decimal("1"), Decimal("2"), Decimal("3")]
        validator.resync_metagraph()
        assert len(validator.scores) == int(validator.metagraph.n)
        # Original values preserved in the overlap region, zeros past.
        assert validator.scores[:3] == [Decimal("1"), Decimal("2"), Decimal("3")]
        assert all(score == Decimal("0") for score in validator.scores[3:])


class TestContextManagerExitSafe:
    def test_exit_on_never_started_validator_is_safe(
        self, validator: _ConcreteValidator
    ) -> None:
        validator.is_running = False
        validator.__exit__(None, None, None)
        assert validator.is_running is False

    def test_context_manager_starts_and_stops_background_thread(
        self, validator: _ConcreteValidator
    ) -> None:
        with validator as entered:
            assert entered is validator
            assert validator.is_running is True
            assert validator.thread is not None

        assert validator.should_exit is True
        assert validator.is_running is False


class TestRunLogging:
    def test_startup_sync_failure_is_redacted_and_stops_worker(
        self,
        validator_config: bt.Config,
        mock_runtime_provider: MockRuntimeProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime_validator = _FailingRuntimeValidator(
            config=validator_config,
            runtime_provider=mock_runtime_provider,
        )
        credential_url = "".join(
            ("wss://worker:credential-sentinel", "@rpc.example.invalid/ws")
        )
        monkeypatch.setattr(
            runtime_validator,
            "sync",
            MagicMock(side_effect=RuntimeError(credential_url)),
        )
        error_log = MagicMock()
        debug_log = MagicMock()
        monkeypatch.setattr(bt.logging, "error", error_log)
        monkeypatch.setattr(bt.logging, "debug", debug_log)

        runtime_validator.run()

        rendered = "\n".join(
            str(call.args[0])
            for call in (*error_log.call_args_list, *debug_log.call_args_list)
        )
        assert runtime_validator.should_exit is True
        assert "credential-sentinel" not in rendered
        assert "<redacted-endpoint>" in rendered

    def test_run_survives_initial_rpc_deferral(
        self,
        validator_config: bt.Config,
        mock_runtime_provider: MockRuntimeProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime_validator = _FailingRuntimeValidator(
            config=validator_config,
            runtime_provider=mock_runtime_provider,
        )
        sync = MagicMock(
            side_effect=RateLimited(
                retry_after_monotonic=2_000.0,
                provider_limited=False,
            )
        )
        monkeypatch.setattr(runtime_validator, "sync", sync)

        async def stop_after_start() -> None:
            runtime_validator.should_exit = True

        monkeypatch.setattr(runtime_validator, "forward", stop_after_start)

        runtime_validator.run()

        sync.assert_called_once_with()

    def test_run_logs_formatted_traceback_instead_of_none(
        self,
        validator_config: bt.Config,
        mock_runtime_provider: MockRuntimeProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime_validator = _FailingRuntimeValidator(
            config=validator_config,
            runtime_provider=mock_runtime_provider,
        )
        credential_url = (
            "wss://validator-user:validator-password"
            "@rpc.example.invalid/"
            "ws?api_key=validator-token"
        )

        async def failing_forward() -> None:
            raise RuntimeError(f"connection failed: {credential_url}")

        monkeypatch.setattr(runtime_validator, "forward", failing_forward)
        # run() now survives forward errors and loops; should_exit makes the
        # always-failing forward terminate after logging the first traceback.
        runtime_validator.should_exit = True

        error_mock = MagicMock()
        debug_mock = MagicMock()
        monkeypatch.setattr(bt.logging, "error", error_mock)
        monkeypatch.setattr(bt.logging, "debug", debug_mock)

        runtime_validator.run()

        debug_messages = [
            args[0] if args else "" for args, _kwargs in debug_mock.call_args_list
        ]

        assert any("Traceback" in str(message) for message in debug_messages)
        assert not any(str(message) == "None" for message in debug_messages)
        rendered = "\n".join(
            str(call.args[0])
            for call in (*error_mock.call_args_list, *debug_mock.call_args_list)
        )
        assert "validator-password" not in rendered
        assert "validator-token" not in rendered
        assert "<redacted-endpoint>" in rendered

    def test_run_survives_transient_forward_exception(
        self,
        validator_config: bt.Config,
        mock_runtime_provider: MockRuntimeProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # _FailingRuntimeValidator inherits the real BaseValidatorNeuron.run();
        # _ConcreteValidator stubs run() out, so it cannot exercise the loop.
        runtime_validator = _FailingRuntimeValidator(
            config=validator_config,
            runtime_provider=mock_runtime_provider,
        )

        calls = {"count": 0}

        async def flaky_forward(
            synapse: bt.Synapse | None = None,
        ) -> bt.Synapse | None:
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("transient boom")
            if calls["count"] >= 3:
                runtime_validator.should_exit = True
            return synapse

        monkeypatch.setattr(runtime_validator, "forward", flaky_forward)

        error_mock = MagicMock()
        monkeypatch.setattr(bt.logging, "error", error_mock)

        runtime_validator.run()

        # The first-iteration error did not end the loop: forward ran again
        # and step advanced past the failure.
        assert calls["count"] >= 3
        assert runtime_validator.step >= 1
        assert error_mock.called


class TestRunSubtensorReconnect:
    def _wedged_validator(
        self,
        validator_config: bt.Config,
        mock_runtime_provider: MockRuntimeProvider,
        monkeypatch: pytest.MonkeyPatch,
        failing_iterations: int,
        total_iterations: int,
    ) -> tuple[_FailingRuntimeValidator, MagicMock]:
        runtime_validator = _FailingRuntimeValidator(
            config=validator_config,
            runtime_provider=mock_runtime_provider,
        )

        async def ok_forward() -> None:
            return None

        monkeypatch.setattr(runtime_validator, "forward", ok_forward)

        state = {"sync_calls": 0}

        def wedged_sync() -> None:
            state["sync_calls"] += 1
            # The first call is run()'s pre-loop registration sync, outside
            # the recovery loop; only in-loop calls simulate the wedge.
            loop_call = state["sync_calls"] - 1
            if loop_call >= total_iterations:
                runtime_validator.should_exit = True
            if 1 <= loop_call <= failing_iterations:
                raise RuntimeError("-32029 rate limited")

        monkeypatch.setattr(runtime_validator, "sync", wedged_sync)
        monkeypatch.setattr(bt.logging, "error", MagicMock())
        monkeypatch.setattr(bt.logging, "debug", MagicMock())

        reconnect_spy = MagicMock(side_effect=mock_runtime_provider.create_subtensor)
        monkeypatch.setattr(mock_runtime_provider, "create_subtensor", reconnect_spy)
        return runtime_validator, reconnect_spy

    def test_sustained_sync_failures_rebuild_subtensor_once(
        self,
        validator_config: bt.Config,
        mock_runtime_provider: MockRuntimeProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 7 failing iterations: rebuild fires at the 5th, the reset counter
        # only reaches 2 before should_exit ends the loop at iteration 7.
        runtime_validator, reconnect_spy = self._wedged_validator(
            validator_config,
            mock_runtime_provider,
            monkeypatch,
            failing_iterations=7,
            total_iterations=7,
        )

        runtime_validator.run()

        assert reconnect_spy.call_count == 1
        assert runtime_validator.gated_subtensor._delegate is (
            mock_runtime_provider.create_subtensor(validator_config)
        )

    def test_failed_rebuild_keeps_loop_alive_and_retries_next_streak(
        self,
        validator_config: bt.Config,
        mock_runtime_provider: MockRuntimeProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 12 failing iterations: the rebuild at streak 5 raises, the loop
        # survives, and the rebuild at streak 10 succeeds before exit at 12.
        runtime_validator, reconnect_spy = self._wedged_validator(
            validator_config,
            mock_runtime_provider,
            monkeypatch,
            failing_iterations=12,
            total_iterations=12,
        )
        original_create = reconnect_spy.side_effect
        rebuilt_subtensor = original_create(validator_config)
        reconnect_spy.side_effect = [
            ConnectionError("archive still down"),
            rebuilt_subtensor,
        ]

        runtime_validator.run()

        assert reconnect_spy.call_count == 2
        assert runtime_validator.gated_subtensor._delegate is rebuilt_subtensor

    def test_failed_rebuild_keeps_existing_transport_open(
        self,
        validator: _ConcreteValidator,
        mock_runtime_provider: MockRuntimeProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        existing_subtensor = validator.subtensor
        close_spy = MagicMock()
        monkeypatch.setattr(existing_subtensor, "close", close_spy)
        credential_url = "".join(
            ("wss://user:password", "@rpc.example.invalid/ws?token=secret")
        )
        monkeypatch.setattr(
            mock_runtime_provider,
            "create_subtensor",
            MagicMock(side_effect=ConnectionError(credential_url)),
        )
        error_mock = MagicMock()
        monkeypatch.setattr(bt.logging, "error", error_mock)

        validator._reconnect_subtensor()

        close_spy.assert_not_called()
        assert validator.subtensor is existing_subtensor
        rendered = str(error_mock.call_args.args[0])
        assert "password" not in rendered
        assert "secret" not in rendered
        assert "<redacted-endpoint>" in rendered

    def test_failures_below_threshold_do_not_reconnect(
        self,
        validator_config: bt.Config,
        mock_runtime_provider: MockRuntimeProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 4 failures then recovery: the streak never reaches the threshold
        # and the successful iteration resets the counter.
        runtime_validator, reconnect_spy = self._wedged_validator(
            validator_config,
            mock_runtime_provider,
            monkeypatch,
            failing_iterations=4,
            total_iterations=6,
        )

        runtime_validator.run()

        assert reconnect_spy.call_count == 0
        assert runtime_validator._consecutive_loop_failures == 0
        assert runtime_validator.step >= 1


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])


class TestLoadStatePartialCorruption:
    def test_partially_valid_state_leaves_all_fields_at_defaults(
        self, validator: _ConcreteValidator
    ) -> None:
        # A file whose early keys load but whose later key is missing must not
        # leave the validator half-restored from a quarantined file.
        state_path = Path(validator.config.neuron.full_path) / "state.npz"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(state_path, step=99, scores=np.array(["0.5", "0.5"]))  # no hotkeys
        before_step = validator.step
        before_scores = validator.scores.copy()
        before_hotkeys = list(validator.hotkeys)

        validator.load_state()

        assert validator.step == before_step
        assert validator.scores == before_scores
        assert validator.hotkeys == before_hotkeys
        assert not state_path.exists()
        assert list(state_path.parent.glob("state.npz.corrupt*"))

    def test_non_finite_legacy_scores_are_ignored_not_quarantined(
        self, validator: _ConcreteValidator
    ) -> None:
        # SQLite EMA state is the sole score authority, so a legacy 'scores'
        # key is never read into self.scores. Even non-finite legacy score
        # bytes must not quarantine the checkpoint: step and hotkeys still
        # restore, self.scores stays at its safe in-memory zeros, and later
        # set_weights never sees a NaN.
        expected_step = validator.step + 7
        expected_hotkeys = list(validator.hotkeys)
        state_path = Path(validator.config.neuron.full_path) / "state.npz"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(
            state_path,
            step=expected_step,
            scores=np.array(["NaN"] * len(validator.scores)),
            hotkeys=np.array(expected_hotkeys),
        )

        validator.load_state()

        assert validator.step == expected_step
        assert validator.hotkeys == expected_hotkeys
        assert all(score.is_finite() for score in validator.scores)
        assert state_path.exists()
        assert not list(state_path.parent.glob("state.npz.corrupt*"))
        validator._normalized_weights()  # must not raise


class TestSelfPacedSync:
    def _paced_validator(
        self,
        validator: _ConcreteValidator,
        monkeypatch: pytest.MonkeyPatch,
        holder: dict,
    ) -> dict:
        monkeypatch.setattr(
            "endure.base.neuron.ttl_get_block", lambda _self: holder["block"]
        )
        validator.config.neuron.epoch_length = 100
        # Chain last_update is frozen far in the past — the abstaining case.
        validator.metagraph.last_update[validator.uid] = 0
        spies = {"resync": MagicMock(), "weights": MagicMock()}
        monkeypatch.setattr(validator, "resync_metagraph", spies["resync"])
        monkeypatch.setattr(validator, "set_weights", spies["weights"])
        monkeypatch.setattr(validator, "save_state", MagicMock())
        monkeypatch.setattr(validator, "check_registered", MagicMock())
        validator.step = 5
        validator.config.neuron.disable_set_weights = False
        # Start from a known seeded state at the current block.
        validator._last_metagraph_sync = holder["block"]
        validator._last_weights_attempt = holder["block"]
        return spies

    def test_frozen_chain_last_update_does_not_resync_every_tick(
        self, validator: _ConcreteValidator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        holder = {"block": 10_000}
        spies = self._paced_validator(validator, monkeypatch, holder)
        epoch = validator.config.neuron.epoch_length

        holder["block"] += 1
        validator.sync()  # one block after our own sync: NOT due, despite
        assert spies["resync"].call_count == 0  # last_update being frozen at 0
        holder["block"] += epoch + 1
        validator.sync()
        assert spies["resync"].call_count == 1
        holder["block"] += 1
        validator.sync()
        assert spies["resync"].call_count == 1
        holder["block"] += epoch + 1
        validator.sync()
        assert spies["resync"].call_count == 2

    def test_weight_attempts_are_epoch_paced_when_abstaining(
        self, validator: _ConcreteValidator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An abstaining validator never advances on-chain last_update, so
        # weight attempts must pace on our own last attempt or they (and the
        # abstain warning) fire every tick forever.
        holder = {"block": 10_000}
        spies = self._paced_validator(validator, monkeypatch, holder)
        epoch = validator.config.neuron.epoch_length

        holder["block"] += 1
        validator.sync()
        assert spies["weights"].call_count == 0
        holder["block"] += epoch + 1
        validator.sync()
        # A due resync wins the shared tick; emission remains due for the next pass.
        assert spies["weights"].call_count == 0
        holder["block"] += 1
        validator.sync()
        assert spies["weights"].call_count == 1
        holder["block"] += 1
        validator.sync()
        assert spies["weights"].call_count == 1
        holder["block"] += epoch + 1
        validator.sync()
        assert spies["weights"].call_count == 1
        holder["block"] += 1
        validator.sync()
        assert spies["weights"].call_count == 2

    def test_deferred_weight_attempt_does_not_advance_pacing_block(
        self, validator: _ConcreteValidator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        holder = {"block": 10_000}
        spies = self._paced_validator(validator, monkeypatch, holder)
        previous_attempt = validator._last_weights_attempt
        validator._last_metagraph_sync = holder["block"] + 1
        spies["weights"].side_effect = RateLimited(
            retry_after_monotonic=20_000.0,
            provider_limited=False,
        )
        holder["block"] += validator.config.neuron.epoch_length + 1

        validator.sync()

        spies["weights"].assert_called_once_with()
        assert validator._last_weights_attempt == previous_attempt

    def test_weight_throttle_counts_toward_reconnect_without_resubmission(
        self, validator: _ConcreteValidator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Given a due weight attempt whose ambiguous result records a provider 429.
        holder = {"block": 10_000}
        spies = self._paced_validator(validator, monkeypatch, holder)
        validator._last_metagraph_sync = holder["block"] + 1

        def record_ambiguous_provider_throttle() -> None:
            validator._consecutive_provider_throttles += 1

        spies["weights"].side_effect = record_ambiguous_provider_throttle
        holder["block"] += validator.config.neuron.epoch_length + 1

        # When sync completes after the weight method handled the ambiguous result.
        validator.sync()

        # Then the successful registration check must not erase that throttle signal.
        spies["weights"].assert_called_once_with()
        assert validator._consecutive_provider_throttles == 1

        reconnect = MagicMock()
        monkeypatch.setattr(validator, "_reconnect_subtensor", reconnect)
        validator.check_registered.side_effect = RateLimited(
            retry_after_monotonic=20_000.0,
            provider_limited=True,
        )

        # When the wedged connection reports two more provider throttles.
        for _ in range(2):
            validator.sync()
            validator._maybe_reconnect_on_provider_throttles()

        # Then the third signal reconnects once without repeating the ambiguous write.
        assert reconnect.call_count == 1
        assert validator._consecutive_provider_throttles == 0
        spies["weights"].assert_called_once_with()


class TestProviderThrottleReconnect:
    def _rewire_gate(
        self, validator: _ConcreteValidator, clock: Callable[[], float]
    ) -> object:
        gate = AdaptiveRpcGate(clock=clock, sleeper=lambda _seconds: None)
        delegate = validator.gated_subtensor._delegate
        validator.rpc_gate = gate
        validator.gated_subtensor = GatedSubtensor(delegate, gate)
        validator.subtensor = validator.gated_subtensor
        return delegate

    def test_sync_counts_real_provider_throttles_and_resets_on_success(
        self,
        validator: _ConcreteValidator,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clock_state = {"now": 0.0}
        delegate = self._rewire_gate(validator, lambda: clock_state["now"])

        def throttled(**_kwargs: object) -> bool:
            raise RuntimeError("-32029 rate limited")

        monkeypatch.setattr(delegate, "is_hotkey_registered", throttled)
        for _ in range(3):
            clock_state["now"] += 100.0
            validator.sync()
        assert validator._consecutive_provider_throttles == 3

        monkeypatch.setattr(delegate, "is_hotkey_registered", lambda **_kwargs: True)
        clock_state["now"] += 100.0
        validator.sync()
        assert validator._consecutive_provider_throttles == 0

    def test_reconnect_fires_only_after_throttle_threshold(
        self,
        validator: _ConcreteValidator,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        reconnect = MagicMock()
        monkeypatch.setattr(validator, "_reconnect_subtensor", reconnect)

        validator._consecutive_provider_throttles = 2
        validator._maybe_reconnect_on_provider_throttles()
        assert reconnect.call_count == 0
        assert validator._consecutive_provider_throttles == 2

        validator._consecutive_provider_throttles = 3
        validator._maybe_reconnect_on_provider_throttles()
        assert reconnect.call_count == 1
        assert validator._consecutive_provider_throttles == 0
