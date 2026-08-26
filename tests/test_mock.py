"""Tests for endure.runtime.mock (Endure's Bittensor mock primitives).

Shared fixtures (mock_wallet, isolate_home, reset_mock_subtensor_state)
live in tests/conftest.py.
"""

from __future__ import annotations

import asyncio
import inspect
import random
from unittest.mock import patch

import bittensor as bt
import pytest

from endure.runtime.mock import (
    MockDendrite,
    MockMetagraph,
    MockSubtensor,
    build_mock_axon,
)

# MockDendrite.forward calls synapse.copy(), which Pydantic v2 emits a
# DeprecationWarning for. pyproject.toml promotes all warnings to errors
# except bittensor's own; this whitelists Pydantic too at test scope
# only. This keeps the test scoped to the runtime mock provider.
pytestmark = pytest.mark.filterwarnings(
    "ignore::pydantic.warnings.PydanticDeprecatedSince20"
)


def _registered_hotkeys(st: MockSubtensor, netuid: int) -> set[str]:
    """Return hotkeys registered on netuid by reading chain_state directly.

    bittensor 10.x MockSubtensor.is_hotkey_registered routes through
    self.query() which returns a MagicMock (always truthy), so we must
    inspect the underlying Keys dict to get ground-truth registration
    state.
    """
    keys_by_uid = st.chain_state["SubtensorModule"]["Keys"].get(netuid, {})
    return {list(history.values())[-1] for history in keys_by_uid.values() if history}


class TestMockSubtensor:
    def test_constructs_with_netuid_and_registers_n_miners(
        self, mock_wallet: bt.Wallet
    ) -> None:
        st = MockSubtensor(netuid=1, n=8, wallet=mock_wallet)
        networks = st.chain_state["SubtensorModule"]["NetworksAdded"]
        assert 1 in networks
        registered = _registered_hotkeys(st, 1)
        assert mock_wallet.hotkey.ss58_address in registered
        assert "miner-hotkey-1" in registered
        assert "miner-hotkey-8" in registered

    def test_reusing_same_wallet_raises_because_hotkey_already_registered(
        self, mock_wallet: bt.Wallet
    ) -> None:
        # bittensor 10.x force_register_neuron raises on a re-registration
        # of the same hotkey. The mock provider does NOT guard against this,
        # so double-construction with the same wallet will raise. This
        # is a known scaffold limitation (documented in the handoff);
        # tests/fixtures must use distinct wallets or clear global state.
        MockSubtensor(netuid=1, n=4, wallet=mock_wallet)
        with pytest.raises(Exception, match="Hotkey already registered"):
            MockSubtensor(netuid=1, n=4, wallet=mock_wallet)

    def test_wallet_none_skips_validator_registration(
        self, mock_wallet: bt.Wallet
    ) -> None:
        st = MockSubtensor(netuid=2, n=3, wallet=None)
        registered = _registered_hotkeys(st, 2)
        assert mock_wallet.hotkey.ss58_address not in registered
        assert "miner-hotkey-1" in registered
        assert len(registered) == 3

    def test_serve_axon_succeeds_cleanly_in_mock_mode(
        self,
        mock_wallet: bt.Wallet,
        mock_validator_config: bt.Config,
        trap_external_ip: dict[str, int],
    ) -> None:
        st = MockSubtensor(netuid=1, n=4, wallet=mock_wallet)
        axon = build_mock_axon(mock_wallet, mock_validator_config)

        response = st.serve_axon(netuid=1, axon=axon)

        assert response.success is True
        assert trap_external_ip["count"] == 0


class TestMockMetagraph:
    def test_constructs_and_patches_axon_endpoints(
        self, mock_wallet: bt.Wallet
    ) -> None:
        st = MockSubtensor(netuid=1, n=4, wallet=mock_wallet)
        mg = MockMetagraph(netuid=1, subtensor=st)

        assert int(mg.n) == 5
        assert len(mg.axons) == 5
        for axon in mg.axons:
            assert axon.ip == "127.0.0.0"
            assert axon.port == 8091

    def test_hotkeys_populated(self, mock_wallet: bt.Wallet) -> None:
        st = MockSubtensor(netuid=1, n=3, wallet=mock_wallet)
        mg = MockMetagraph(netuid=1, subtensor=st)
        assert len(mg.hotkeys) == 4
        assert mock_wallet.hotkey.ss58_address in mg.hotkeys

    def test_validator_permit_is_iterable(self, mock_wallet: bt.Wallet) -> None:
        st = MockSubtensor(netuid=1, n=2, wallet=mock_wallet)
        mg = MockMetagraph(netuid=1, subtensor=st)
        assert len(mg.validator_permit) == 3


class _SynapseWithDummy(bt.Synapse):
    """A Synapse subclass that carries dummy_input/dummy_output attributes.

    The inherited MockDendrite.forward explicitly mutates s.dummy_output
    and reads s.dummy_input; those attributes don't exist on a vanilla
    bt.Synapse. We subclass here to give the template mock exactly the
    payload shape it expects, without patching production code.
    """

    dummy_input: int = 0
    dummy_output: int = 0


class TestMockDendrite:
    def test_forward_signature_uses_none_default_for_synapse(self) -> None:
        parameter = inspect.signature(MockDendrite.forward).parameters["synapse"]
        assert parameter.default is None

    def test_str_format(self, mock_wallet: bt.Wallet) -> None:
        d = MockDendrite(wallet=mock_wallet)
        assert str(d) == f"MockDendrite({mock_wallet.hotkey.ss58_address})"

    def test_forward_returns_list_of_correct_length(
        self, mock_wallet: bt.Wallet
    ) -> None:
        random.seed(42)
        d = MockDendrite(wallet=mock_wallet)
        axons = [
            bt.AxonInfo(
                version=1,
                ip="127.0.0.0",
                port=8091,
                ip_type=4,
                hotkey=f"hk-{i}",
                coldkey="ck",
                protocol=4,
                placeholder1=0,
                placeholder2=0,
            )
            for i in range(3)
        ]
        syn = _SynapseWithDummy(dummy_input=7)
        results = asyncio.run(d.forward(axons, syn, timeout=12, deserialize=False))
        assert isinstance(results, list)
        assert len(results) == 3

    def test_forward_without_synapse_uses_plain_synapse_success_path(
        self, mock_wallet: bt.Wallet
    ) -> None:
        d = MockDendrite(wallet=mock_wallet)
        axons = [
            bt.AxonInfo(
                version=1,
                ip="127.0.0.0",
                port=8091,
                ip_type=4,
                hotkey="hk-0",
                coldkey="ck",
                protocol=4,
                placeholder1=0,
                placeholder2=0,
            )
        ]

        results = asyncio.run(d.forward(axons))

        assert isinstance(results, list)
        assert len(results) == 1
        assert isinstance(results[0], bt.Synapse)
        assert results[0].dendrite.status_code == 200
        assert results[0].dendrite.process_time is not None

    def test_forward_does_not_mutate_caller_owned_synapse(
        self, mock_wallet: bt.Wallet
    ) -> None:
        d = MockDendrite(wallet=mock_wallet)
        axons = [
            bt.AxonInfo(
                version=1,
                ip="127.0.0.0",
                port=8091,
                ip_type=4,
                hotkey=f"hk-{i}",
                coldkey="ck",
                protocol=4,
                placeholder1=0,
                placeholder2=0,
            )
            for i in range(2)
        ]
        syn = _SynapseWithDummy(dummy_input=7)
        original_process_time = syn.dendrite.process_time

        results = asyncio.run(d.forward(axons, syn, timeout=12, deserialize=False))

        assert syn.dendrite.process_time == original_process_time
        assert all(result is not syn for result in results)
        assert all(result.dendrite.process_time is not None for result in results)

    def test_forward_streaming_raises_not_implemented(
        self, mock_wallet: bt.Wallet
    ) -> None:
        d = MockDendrite(wallet=mock_wallet)
        with pytest.raises(NotImplementedError):
            asyncio.run(
                d.forward(axons=[], synapse=_SynapseWithDummy(), streaming=True)
            )

    def test_forward_timeout_sets_408(self, mock_wallet: bt.Wallet) -> None:
        # Force random.random to always exceed timeout -> takes the
        # timeout branch in runtime/mock.py -> status 408.
        d = MockDendrite(wallet=mock_wallet)
        axons = [
            bt.AxonInfo(
                version=1,
                ip="127.0.0.0",
                port=8091,
                ip_type=4,
                hotkey="hk-0",
                coldkey="ck",
                protocol=4,
                placeholder1=0,
                placeholder2=0,
            )
        ]
        syn = _SynapseWithDummy(dummy_input=3)
        with patch("endure.runtime.mock.random") as fake_random:
            fake_random.random.return_value = 99.0
            results = asyncio.run(d.forward(axons, syn, timeout=1.0, deserialize=False))
        assert len(results) == 1
        assert results[0].dendrite.status_code == 408


class TestMockAxon:
    def test_build_mock_axon_uses_loopback_without_external_ip_lookup(
        self,
        mock_wallet: bt.Wallet,
        mock_miner_config: bt.Config,
        trap_external_ip: dict[str, int],
    ) -> None:
        axon = build_mock_axon(mock_wallet, mock_miner_config)

        assert axon.ip == "127.0.0.1"
        assert axon.external_ip == "127.0.0.1"
        assert axon.external_port == mock_miner_config.axon.port
        assert trap_external_ip["count"] == 0


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
