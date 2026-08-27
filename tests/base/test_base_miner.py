"""Tests for endure.base.miner.BaseMinerNeuron.

Constructor + resync_metagraph. run() / run_in_background_thread are
Slice 2 territory and intentionally skipped.

Note: we deliberately do NOT use `from __future__ import annotations`
here. bittensor 10.x Axon.attach introspects forward_fn's first-param
annotation at runtime and calls issubclass() on it; PEP-563 lazy
annotations turn that annotation into a string and break the check.
"""

import threading
from typing import Tuple
from unittest.mock import MagicMock

import bittensor as bt
import pytest

from endure.base.miner import BaseMinerNeuron
from endure.runtime.mock import MockRuntimeProvider

pytestmark = pytest.mark.filterwarnings(
    "ignore::pydantic.warnings.PydanticDeprecatedSince20"
)


class _ConcreteMiner(BaseMinerNeuron):
    """Minimal BaseMinerNeuron with stub forward/blacklist/priority.

    BaseMinerNeuron.__init__ attaches self.forward, self.blacklist,
    self.priority to the axon; all three must exist on the subclass
    even though none are called during construction itself.
    """

    async def forward(self, synapse: bt.Synapse) -> bt.Synapse:
        return synapse

    async def blacklist(self, synapse: bt.Synapse) -> Tuple[bool, str]:
        return False, ""

    async def priority(self, synapse: bt.Synapse) -> float:
        return 0.0

    def run(self) -> None:
        return None


class _FailingRuntimeMiner(BaseMinerNeuron):
    """Uses the inherited BaseMinerNeuron.run() to exercise loop resilience."""

    async def forward(self, synapse: bt.Synapse) -> bt.Synapse:
        return synapse

    async def blacklist(self, synapse: bt.Synapse) -> Tuple[bool, str]:
        return False, ""

    async def priority(self, synapse: bt.Synapse) -> float:
        return 0.0


@pytest.fixture
def miner(
    mock_miner_config: bt.Config,
    mock_runtime_provider: MockRuntimeProvider,
    trap_external_ip: dict[str, int],
) -> _ConcreteMiner:
    assert trap_external_ip["count"] == 0
    return _ConcreteMiner(
        config=mock_miner_config,
        runtime_provider=mock_runtime_provider,
    )


class TestConstructor:
    def test_wires_axon_and_runners(
        self,
        miner: _ConcreteMiner,
        trap_external_ip: dict[str, int],
    ) -> None:
        assert miner.axon is not None
        assert trap_external_ip["count"] == 0
        assert miner.should_exit is False
        assert miner.is_running is False
        assert miner.thread is None
        assert miner.lock is not None
        assert miner.neuron_type == "MinerNeuron"

    def test_inherits_base_neuron_wiring(self, miner: _ConcreteMiner) -> None:
        assert miner.wallet is not None
        assert miner.subtensor is not None
        assert miner.metagraph is not None
        assert miner.uid is not None


class TestResyncMetagraph:
    def test_calls_metagraph_sync_with_subtensor(self, miner: _ConcreteMiner) -> None:
        fake_mg = MagicMock()
        miner.metagraph = fake_mg
        miner.resync_metagraph()
        fake_mg.sync.assert_called_once_with(subtensor=miner.subtensor)


class TestSyncPacing:
    def test_not_due_before_epoch_length_blocks_elapse(
        self, miner: _ConcreteMiner
    ) -> None:
        miner.subtensor = MagicMock()
        miner.subtensor.get_current_block.return_value = 50
        miner.config.neuron.epoch_length = 100
        assert miner._due_for_sync(last_sync_block=0) is False

    def test_due_after_epoch_length_blocks_elapse(self, miner: _ConcreteMiner) -> None:
        miner.subtensor = MagicMock()
        miner.subtensor.get_current_block.return_value = 100
        miner.config.neuron.epoch_length = 100
        assert miner._due_for_sync(last_sync_block=0) is True

    def test_pacing_ignores_metagraph_last_update(self, miner: _ConcreteMiner) -> None:
        # Regression: metagraph.last_update[uid] never advances for a neuron
        # that does not set weights (a miner), so the old
        # block - last_update[uid] pacing was permanently past-due and busy-
        # synced the chain. Pacing must key on the miner's own last sync block.
        miner.subtensor = MagicMock()
        miner.subtensor.get_current_block.return_value = 10_000
        miner.metagraph = MagicMock()
        miner.metagraph.last_update = [0]
        miner.uid = 0
        miner.config.neuron.epoch_length = 100
        # Synced 50 blocks ago — not yet due despite a huge block-vs-last_update.
        assert miner._due_for_sync(last_sync_block=9_950) is False


class TestContextManagerWithoutRunning:
    def test_exit_on_never_started_miner_is_safe(self, miner: _ConcreteMiner) -> None:
        # __exit__ calls stop_run_thread which returns early when
        # is_running is False, so entering-then-exiting a miner we
        # never started must not raise.
        miner.is_running = False
        miner.__exit__(None, None, None)
        assert miner.is_running is False

    def test_context_manager_starts_and_stops_background_thread(
        self, miner: _ConcreteMiner
    ) -> None:
        miner.axon.stop = MagicMock()
        miner.subtensor.close = MagicMock()
        with miner as entered:
            assert entered is miner
            assert miner.is_running is True
            assert miner.thread is not None

        assert miner.should_exit is True
        assert miner.is_running is False
        assert miner.thread is None
        miner.axon.stop.assert_called_once_with()
        miner.subtensor.close.assert_called_once_with()

    def test_shutdown_wakes_and_joins_a_waiting_worker(
        self, miner: _ConcreteMiner
    ) -> None:
        miner.axon.stop = MagicMock()
        worker = threading.Thread(
            target=miner._shutdown_event.wait,
            args=(60,),
            daemon=True,
        )
        miner.thread = worker
        miner.is_running = True
        worker.start()

        miner.stop_run_thread()

        assert not worker.is_alive()
        assert miner.thread is None
        assert miner.is_running is False
        miner.axon.stop.assert_called_once_with()

    def test_shutdown_timeout_keeps_worker_state_truthful(
        self, miner: _ConcreteMiner
    ) -> None:
        miner.axon.stop = MagicMock()
        worker = MagicMock(spec=threading.Thread)
        worker.is_alive.return_value = True
        miner.thread = worker
        miner.is_running = True

        with pytest.raises(RuntimeError, match="miner loop did not stop"):
            miner.stop_run_thread()

        assert miner.thread is worker
        assert miner.is_running is True
        miner.axon.stop.assert_called_once_with()

    def test_miner_set_weights_hook_is_an_explicit_noop(
        self, miner: _ConcreteMiner
    ) -> None:
        assert miner.set_weights() is None


class TestRunLoopResilience:
    def test_startup_failure_is_redacted_and_stops_worker(
        self,
        mock_miner_config: bt.Config,
        mock_runtime_provider: MockRuntimeProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        miner = _FailingRuntimeMiner(
            config=mock_miner_config,
            runtime_provider=mock_runtime_provider,
        )
        credential_url = "".join(
            ("wss://worker:credential-sentinel", "@rpc.example.invalid/ws")
        )
        monkeypatch.setattr(
            miner, "sync", MagicMock(side_effect=RuntimeError(credential_url))
        )
        error_log = MagicMock()
        debug_log = MagicMock()
        monkeypatch.setattr(bt.logging, "error", error_log)
        monkeypatch.setattr(bt.logging, "debug", debug_log)

        miner.run()

        rendered = "\n".join(
            str(call.args[0])
            for call in (*error_log.call_args_list, *debug_log.call_args_list)
        )
        assert miner.should_exit is True
        assert "credential-sentinel" not in rendered
        assert "<redacted-endpoint>" in rendered

    def test_run_survives_transient_sync_exception(
        self,
        mock_miner_config: bt.Config,
        mock_runtime_provider: MockRuntimeProvider,
        trap_external_ip: dict[str, int],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        miner = _FailingRuntimeMiner(
            config=mock_miner_config,
            runtime_provider=mock_runtime_provider,
        )
        monkeypatch.setattr(miner.axon, "serve", MagicMock())
        monkeypatch.setattr(miner.axon, "start", MagicMock())
        # Advance the block on every read so the epoch-wait inner loop clears
        # each iteration (pacing keys on blocks since our own last sync).
        block_counter = {"n": 0}

        def advancing_block(_self: object) -> int:
            block_counter["n"] += 1_000
            return block_counter["n"]

        monkeypatch.setattr("endure.base.neuron.ttl_get_block", advancing_block)
        miner.config.neuron.epoch_length = 1

        calls = {"count": 0}

        def flaky_sync() -> None:
            calls["count"] += 1
            # call 1 is the pre-loop registration sync; fail the first in-loop
            # sync, then stop once the loop has demonstrably recovered.
            if calls["count"] == 2:
                raise RuntimeError(
                    "wss://miner-user:miner-password"
                    "@rpc.example.invalid/"
                    "ws?api_key=miner-token"
                )
            if calls["count"] >= 4:
                miner.should_exit = True

        monkeypatch.setattr(miner, "sync", flaky_sync)

        error_mock = MagicMock()
        debug_mock = MagicMock()
        monkeypatch.setattr(bt.logging, "error", error_mock)
        monkeypatch.setattr(bt.logging, "debug", debug_mock)

        miner.run()

        # The first in-loop failure did not end the loop: sync ran again and
        # step advanced past the failure instead of returning silently.
        assert calls["count"] >= 4
        assert miner.step >= 1
        assert error_mock.called
        rendered = "\n".join(
            str(call.args[0])
            for call in (*error_mock.call_args_list, *debug_mock.call_args_list)
        )
        assert "miner-password" not in rendered
        assert "miner-token" not in rendered
        assert "<redacted-endpoint>" in rendered


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])


class TestSyncFailureThrottle:
    def test_sync_failure_retries_are_throttled(
        self,
        mock_miner_config: bt.Config,
        mock_runtime_provider: MockRuntimeProvider,
        trap_external_ip: dict[str, int],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # When sync() fails, last_sync_block never advances, so the wait loop's
        # sleep is skipped and the retry must be throttled explicitly — or a
        # dead chain endpoint gets hammered in a hot loop.
        miner = _FailingRuntimeMiner(
            config=mock_miner_config,
            runtime_provider=mock_runtime_provider,
        )
        monkeypatch.setattr(miner.axon, "serve", MagicMock())
        monkeypatch.setattr(miner.axon, "start", MagicMock())
        block_counter = {"n": 0}

        def advancing_block(_self: object) -> int:
            block_counter["n"] += 1_000
            return block_counter["n"]

        monkeypatch.setattr("endure.base.neuron.ttl_get_block", advancing_block)
        miner.config.neuron.epoch_length = 1

        sleeps: list[float] = []
        monkeypatch.setattr(
            miner._shutdown_event, "wait", lambda seconds: sleeps.append(seconds)
        )

        calls = {"count": 0}

        def failing_sync() -> None:
            calls["count"] += 1
            if calls["count"] == 1:
                return  # pre-loop registration sync succeeds
            if calls["count"] >= 4:
                miner.should_exit = True
            raise RuntimeError("chain down")

        monkeypatch.setattr(miner, "sync", failing_sync)
        monkeypatch.setattr(bt.logging, "error", MagicMock())

        miner.run()

        # Failures at calls 2 and 3 continued the loop; each must have slept.
        assert len(sleeps) >= 2
