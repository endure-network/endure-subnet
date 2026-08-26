from __future__ import annotations

from unittest.mock import MagicMock

from endure.base.validator import BaseValidatorNeuron
from endure.protocol.version_contract import CURRENT_VERSION_KEY
from endure.storage.repository import _weight_intent_hash


class _IntentNeuron(BaseValidatorNeuron):
    def forward(self) -> None:
        raise NotImplementedError("test double — run loop is outside this test")


def _neuron(spec_version: int) -> BaseValidatorNeuron:
    neuron = _IntentNeuron.__new__(_IntentNeuron)
    neuron.spec_version = spec_version
    neuron.uid = 0
    neuron.config = MagicMock()
    neuron.config.netuid = 1
    neuron.wallet = MagicMock()
    neuron.wallet.hotkey.ss58_address = "validator-hotkey"
    neuron.metagraph = MagicMock()
    neuron.metagraph.hotkeys = ["validator-hotkey", "miner-hotkey"]
    neuron.metagraph.last_update = [10, 0]
    neuron.subtensor = MagicMock()
    neuron.subtensor.get_current_block.return_value = 12
    neuron.gated_subtensor = MagicMock()
    neuron.gated_subtensor.get_block_hash.return_value = "genesis"
    neuron.gated_subtensor.commit_reveal_enabled.return_value = True
    return neuron


def test_weight_intent_uses_protocol_key_not_package_spec_version() -> None:
    unrelated_package_spec_version = CURRENT_VERSION_KEY + 1000
    intent = _neuron(unrelated_package_spec_version)._prepare_weight_intent(
        [1], [65535]
    )

    assert intent.protocol_version_key == CURRENT_VERSION_KEY
    assert intent.protocol_version_key != unrelated_package_spec_version
    assert intent.intent_hash == _weight_intent_hash(
        chain_identity="genesis",
        netuid=1,
        validator_uid=0,
        validator_hotkey="validator-hotkey",
        targets=((1, "miner-hotkey", 65535),),
        protocol_version_key=CURRENT_VERSION_KEY,
    )
    assert intent.intent_hash.startswith(f"{CURRENT_VERSION_KEY}:")
