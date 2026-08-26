"""Tests for endure.base.neuron.BaseNeuron.

BaseNeuron is abstract. We define a
minimal concrete subclass for instantiation tests, and cover the non-
chain predicates + lifecycle (check_registered, sync, should_sync_metagraph,
should_set_weights) that don't require a running axon.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import bittensor as bt
import pytest

from endure.base.neuron import BaseNeuron
from endure.runtime.mock import MockRuntimeProvider

pytestmark = pytest.mark.filterwarnings(
    "ignore::pydantic.warnings.PydanticDeprecatedSince20"
)


class _ConcreteNeuron(BaseNeuron):
    """Minimal instantiable BaseNeuron — no-op forward/run."""

    neuron_type = "TestNeuron"

    def run(self) -> None:
        return None

    def resync_metagraph(self) -> None:
        return None

    def set_weights(self) -> None:
        return None


@pytest.fixture
def neuron(
    mock_config_base: bt.Config,
    mock_runtime_provider: MockRuntimeProvider,
) -> _ConcreteNeuron:
    return _ConcreteNeuron(
        config=mock_config_base,
        runtime_provider=mock_runtime_provider,
    )


class TestConstructor:
    def test_wires_wallet_subtensor_metagraph_and_uid(
        self, neuron: _ConcreteNeuron
    ) -> None:
        assert neuron.wallet is not None
        assert neuron.subtensor is not None
        assert neuron.metagraph is not None
        assert neuron.step == 0
        # uid is the wallet's position in metagraph.hotkeys.
        assert neuron.uid == neuron.metagraph.hotkeys.index(
            neuron.wallet.hotkey.ss58_address
        )

    def test_startup_logs_never_include_endpoint_credentials(
        self,
        mock_config_base: bt.Config,
        mock_runtime_provider: MockRuntimeProvider,
    ) -> None:
        secret = "super-secret-token"
        mock_config_base.subtensor.chain_endpoint = (
            f"wss://operator:{secret}@rpc.example.org:9944/private?token={secret}"
        )
        mock_config_base.endure.market_data_endpoint = (
            f"https://operator:{secret}@market.example.org/feed?token={secret}"
        )

        with patch.object(bt.logging, "info") as info:
            _ConcreteNeuron(
                config=mock_config_base,
                runtime_provider=mock_runtime_provider,
            )

        rendered = "\n".join(str(call.args[0]) for call in info.call_args_list)
        assert "wss://rpc.example.org:9944" in rendered
        assert "https://market.example.org" in rendered
        assert secret not in rendered
        assert "/private" not in rendered


class TestCheckRegistered:
    def test_passes_silently_when_hotkey_registered(
        self, neuron: _ConcreteNeuron
    ) -> None:
        # Constructor already called check_registered. If we got here,
        # it passed. Run it explicitly once more.
        neuron.check_registered()

    def test_exits_when_hotkey_not_registered(self, neuron: _ConcreteNeuron) -> None:
        # In v10.x is_hotkey_registered returns a MagicMock (truthy). To
        # force the unregistered path, directly stub the call.
        neuron.subtensor = MagicMock()
        neuron.subtensor.is_hotkey_registered.return_value = False
        with pytest.raises(SystemExit):
            neuron.check_registered()


class TestShouldSyncMetagraph:
    def test_first_check_seeds_and_is_not_due(
        self, neuron: _ConcreteNeuron, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The metagraph is freshly built at construction, so the first pacing
        # check seeds from the current block and owes the next resync one
        # epoch later — not immediately.
        holder = {"block": 500}
        monkeypatch.setattr(
            "endure.base.neuron.ttl_get_block", lambda _self: holder["block"]
        )
        assert neuron.should_sync_metagraph() is False

    def test_due_after_epoch_length_blocks_since_own_sync(
        self, neuron: _ConcreteNeuron, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pacing keys on our own last sync, NOT metagraph.last_update[uid]
        # (which freezes for neurons that never land weights on chain).
        holder = {"block": 500}
        monkeypatch.setattr(
            "endure.base.neuron.ttl_get_block", lambda _self: holder["block"]
        )
        neuron.metagraph = MagicMock()
        neuron.metagraph.last_update = [0]
        neuron.uid = 0
        assert neuron.should_sync_metagraph() is False  # seeds at 500
        holder["block"] += neuron.config.neuron.epoch_length
        assert neuron.should_sync_metagraph() is False  # boundary: not yet due
        holder["block"] += 1
        assert neuron.should_sync_metagraph() is True


class TestShouldSetWeights:
    def test_false_at_step_zero(self, neuron: _ConcreteNeuron) -> None:
        assert neuron.step == 0
        assert neuron.should_set_weights() is False

    def test_false_when_disable_set_weights(self, neuron: _ConcreteNeuron) -> None:
        neuron.step = 10
        neuron.config.neuron.disable_set_weights = True
        assert neuron.should_set_weights() is False

    def test_false_for_miner_type_even_after_epoch(
        self, neuron: _ConcreteNeuron
    ) -> None:
        neuron.step = 10
        neuron.neuron_type = "MinerNeuron"
        neuron.config.neuron.disable_set_weights = False
        neuron.metagraph = MagicMock()
        neuron.metagraph.last_update = [0]
        neuron.uid = 0
        neuron.subtensor = MagicMock()
        neuron.subtensor.get_current_block.return_value = 10_000
        assert neuron.should_set_weights() is False

    def test_true_for_validator_after_epoch_elapsed(
        self, neuron: _ConcreteNeuron, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        neuron.step = 10
        neuron.neuron_type = "ValidatorNeuron"
        neuron.config.neuron.disable_set_weights = False
        holder = {"block": 10_000}
        monkeypatch.setattr(
            "endure.base.neuron.ttl_get_block", lambda _self: holder["block"]
        )
        assert neuron.should_set_weights() is False  # first check seeds
        holder["block"] += neuron.config.neuron.epoch_length + 1
        assert neuron.should_set_weights() is True


class TestSaveAndLoadStateNoOps:
    def test_save_state_is_noop(self, neuron: _ConcreteNeuron) -> None:
        neuron.save_state()

    def test_load_state_is_noop(self, neuron: _ConcreteNeuron) -> None:
        neuron.load_state()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
