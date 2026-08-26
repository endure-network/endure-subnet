"""Production-path attribute-access smoke tests.

Most other runtime tests use the mock runtime provider or runtime.mode=mock,
which means the live branch of BaseNeuron construction and validator/miner
runtime wiring still needs an explicit runtime smoke test.

These tests patch bt.Wallet, bt.Subtensor, bt.Dendrite, bt.Axon to
MagicMock factories and assert that BaseNeuron / BaseValidatorNeuron /
BaseMinerNeuron successfully construct in non-mock mode. Any future
regression that reverts an attribute back to lowercase (or otherwise
breaks the non-mock production path) will fail here at the first
attribute lookup, BEFORE the bug ships to testnet.

The pyright type-stub guardrail (typings/bittensor/__init__.pyi) is the
first line of defense; this is the runtime backstop.
"""

from typing import Tuple
from unittest.mock import MagicMock, patch

import bittensor as bt
import pytest


def _patched_chain_primitives(hotkey_ss58: str = "5Test"):
    """Return a stack of patches that turn every bt.X chain class into
    a MagicMock returning a fully-attributed dummy object.

    Targets the exact attribute-access path BaseNeuron.__init__ walks:
    bt.Wallet().hotkey.ss58_address, bt.Subtensor().is_hotkey_registered,
    bt.Subtensor().metagraph(netuid).hotkeys.index(...),
    bt.Subtensor().chain_endpoint, bt.Dendrite(), bt.Axon().attach().
    """
    fake_wallet = MagicMock()
    fake_wallet.hotkey.ss58_address = hotkey_ss58

    fake_metagraph = MagicMock()
    fake_metagraph.hotkeys = [hotkey_ss58]
    fake_metagraph.n = 1
    fake_metagraph.last_update = [0]

    fake_subtensor = MagicMock()
    fake_subtensor.is_hotkey_registered.return_value = True
    fake_subtensor.metagraph.return_value = fake_metagraph
    fake_subtensor.chain_endpoint = "ws://test-mock-endpoint"
    fake_subtensor.get_current_block.return_value = 0

    return [
        patch("bittensor.Wallet", return_value=fake_wallet),
        patch("bittensor.Subtensor", return_value=fake_subtensor),
        patch("bittensor.Dendrite", return_value=MagicMock()),
        patch("bittensor.Axon", return_value=MagicMock()),
    ]


class _ConcreteMiner:
    """Concrete miner subclass — only used inside test functions to avoid
    triggering BaseMinerNeuron's class-level setup at import time."""


class _ConcreteValidator:
    """Concrete validator subclass — only used inside test functions."""


def test_base_miner_production_path_constructor_uses_only_capitalcase_bittensor(
    production_miner_config: bt.Config,
) -> None:
    """Reproduces the day-1 bug: any reference to bt.wallet (lowercase) /
    bt.subtensor / bt.MockWallet in BaseNeuron.__init__'s non-mock branch
    will raise AttributeError here. With current code we expect a clean
    construction.
    """
    from endure.base.miner import BaseMinerNeuron

    class M(BaseMinerNeuron):
        async def forward(self, synapse: bt.Synapse) -> bt.Synapse:
            return synapse

        async def blacklist(self, synapse: bt.Synapse) -> Tuple[bool, str]:
            return False, ""

        async def priority(self, synapse: bt.Synapse) -> float:
            return 0.0

        def run(self) -> None:
            return None

    patches = _patched_chain_primitives()
    for p in patches:
        p.start()
    try:
        miner = M(config=production_miner_config)
        assert miner.wallet is not None
        assert miner.subtensor is not None
        assert miner.metagraph is not None
        assert miner.uid == 0
    finally:
        for p in patches:
            p.stop()


def test_base_validator_production_path_constructor_uses_only_capitalcase_bittensor(
    production_validator_config: bt.Config,
) -> None:
    """Same intent as the miner test, on the validator construction path.
    Catches lowercase regressions in BaseValidatorNeuron.serve_axon
    (bt.Axon, bt.Dendrite) and the inherited BaseNeuron.__init__ branch.
    """
    from endure.base.validator import BaseValidatorNeuron

    production_validator_config.neuron.axon_off = True
    production_validator_config.neuron.disable_set_weights = True

    class V(BaseValidatorNeuron):
        async def forward(self) -> None:
            return None

        def run(self) -> None:
            return None

    patches = _patched_chain_primitives()
    for p in patches:
        p.start()
    try:
        validator = V(config=production_validator_config)
        assert validator.wallet is not None
        assert validator.subtensor is not None
        assert validator.metagraph is not None
        assert validator.dendrite is not None
    finally:
        for p in patches:
            p.stop()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
