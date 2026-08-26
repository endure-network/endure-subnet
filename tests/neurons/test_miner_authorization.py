from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch

import bittensor as bt

from endure.assessment.schemas.subnet_alpha_risk import RISK_SCHEMA_ID


@contextmanager
def _patched_chain() -> Generator[None, None, None]:
    fake_wallet = MagicMock()
    fake_wallet.hotkey.ss58_address = "5Test"
    fake_metagraph = MagicMock()
    fake_metagraph.hotkeys = ["5Test"]
    fake_metagraph.n = 1
    fake_metagraph.axons = []
    fake_metagraph.validator_permit = []
    fake_subtensor = MagicMock()
    fake_subtensor.is_hotkey_registered.return_value = True
    fake_subtensor.metagraph.return_value = fake_metagraph
    fake_subtensor.chain_endpoint = "ws://test-mock-endpoint"
    fake_subtensor.get_current_block.return_value = 0
    with ExitStack() as stack:
        stack.enter_context(patch("bittensor.Wallet", return_value=fake_wallet))
        stack.enter_context(patch("bittensor.Subtensor", return_value=fake_subtensor))
        stack.enter_context(patch("bittensor.Dendrite", return_value=MagicMock()))
        stack.enter_context(patch("bittensor.Axon", return_value=MagicMock()))
        yield


def _caller(hotkey: str) -> bt.Synapse:
    synapse = bt.Synapse()
    synapse.dendrite.hotkey = hotkey
    return synapse


def _metagraph() -> MagicMock:
    metagraph = MagicMock()
    metagraph.hotkeys = ["hk-validator", "hk-miner"]
    metagraph.validator_permit = [True, False]
    return metagraph


async def test_mock_miner_allows_registered_non_validator_for_development(
    mock_miner_config: bt.Config,
) -> None:
    from neurons.miner import Miner

    miner = Miner(config=mock_miner_config)
    miner.metagraph = _metagraph()

    blocked, reason = await miner.blacklist(_caller("hk-miner"))

    assert blocked is False
    assert reason == "Hotkey recognized"


async def test_live_miner_rejects_registered_non_validator(
    production_miner_config: bt.Config,
) -> None:
    from neurons.miner import Miner

    production_miner_config.endure.active_schema = RISK_SCHEMA_ID
    production_miner_config.endure.serving_stage = "testnet"
    production_miner_config.subtensor.chain_endpoint = "test"
    production_miner_config.subtensor.network = "test"
    original_force_validator_permit = (
        production_miner_config.blacklist.force_validator_permit
    )
    with _patched_chain():
        miner = Miner(config=production_miner_config)
    miner.metagraph = _metagraph()

    validator_blocked, _ = await miner.blacklist(_caller("hk-validator"))
    miner_blocked, reason = await miner.blacklist(_caller("hk-miner"))

    assert validator_blocked is False
    assert miner_blocked is True
    assert reason == "Non-validator hotkey"
    assert (
        production_miner_config.blacklist.force_validator_permit
        is original_force_validator_permit
    )
    assert miner.config.blacklist.force_validator_permit is True


async def test_live_miner_rejects_unregistered_override(
    production_miner_config: bt.Config,
) -> None:
    from neurons.miner import Miner

    production_miner_config.endure.active_schema = RISK_SCHEMA_ID
    production_miner_config.endure.serving_stage = "testnet"
    production_miner_config.subtensor.chain_endpoint = "test"
    production_miner_config.subtensor.network = "test"
    production_miner_config.blacklist.allow_non_registered = True
    with _patched_chain():
        miner = Miner(config=production_miner_config)
    miner.metagraph = _metagraph()

    blocked, reason = await miner.blacklist(_caller("hk-unknown"))

    assert blocked is True
    assert reason == "Unrecognized hotkey"
