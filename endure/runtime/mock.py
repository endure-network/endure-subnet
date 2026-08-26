from __future__ import annotations

import asyncio
import random
import time
from typing import List
from unittest.mock import patch

import bittensor as bt
from bittensor.core.settings import version_as_int
from bittensor.core.types import ExtrinsicResponse
from bittensor.utils import networking
from bittensor_wallet.mock import get_mock_wallet

from endure.runtime.types import BaseRuntimeComponents

MOCK_AXON_IP = "127.0.0.1"


def build_mock_axon(wallet: bt.Wallet, config: bt.Config) -> bt.Axon:
    return bt.Axon(
        wallet=wallet,
        config=config,
        ip=MOCK_AXON_IP,
        external_ip=MOCK_AXON_IP,
        external_port=config.axon.port,
    )


class MockSubtensor(bt.MockSubtensor):
    def __init__(self, netuid, n=16, wallet=None, network="mock"):
        super().__init__(network=network)

        # bittensor 10.x MockSubtensor.subnet_exists goes through
        # self.query() which returns a MagicMock (always truthy), so the
        # original `if not subnet_exists` guard would never trigger and
        # create_subnet (which now RAISES on double-create in v10.x)
        # would blow up. Read the underlying chain_state directly and
        # only create the subnet if it's truly missing.
        if netuid not in self.chain_state["SubtensorModule"]["NetworksAdded"]:
            self.create_subnet(netuid)

        # Register ourself (the validator) as a neuron at uid=0
        if wallet is not None:
            self.force_register_neuron(
                netuid=netuid,
                hotkey_ss58=wallet.hotkey.ss58_address,
                coldkey_ss58=wallet.coldkey.ss58_address,
                balance=100000,
                stake=100000,
            )

        # Register n mock neurons who will be miners
        for i in range(1, n + 1):
            self.force_register_neuron(
                netuid=netuid,
                hotkey_ss58=f"miner-hotkey-{i}",
                coldkey_ss58="mock-coldkey",
                balance=100000,
                stake=100000,
            )

    def serve_axon(
        self,
        netuid: int,
        axon: bt.Axon,
        certificate=None,
        *,
        mev_protection: bool = False,
        period: int | None = None,
        raise_error: bool = False,
        wait_for_inclusion: bool = True,
        wait_for_finalization: bool = True,
        wait_for_revealed_execution: bool = True,
    ) -> ExtrinsicResponse:
        del certificate
        del mev_protection
        del period
        del wait_for_inclusion
        del wait_for_finalization
        del wait_for_revealed_execution

        try:
            hotkey = axon.wallet.hotkey.ss58_address
            if netuid not in self.chain_state["SubtensorModule"]["NetworksAdded"]:
                raise Exception("Subnet does not exist")
            if hotkey not in self.chain_state["SubtensorModule"]["Axons"][netuid]:
                raise Exception("Hotkey not registered")

            self.chain_state["SubtensorModule"]["Axons"][netuid][hotkey][
                self.block_number
            ] = {
                "block": self.block_number,
                "version": version_as_int,
                "ip": networking.ip_to_int(axon.external_ip),
                "port": axon.external_port,
                "ip_type": networking.ip_version(axon.external_ip),
                "protocol": 4,
                "placeholder1": 0,
                "placeholder2": 0,
            }

            return ExtrinsicResponse(
                success=True,
                message="Mock axon registration skipped",
                data={
                    "external_ip": axon.external_ip,
                    "external_port": axon.external_port,
                    "axon": axon,
                },
            )
        except Exception as error:
            return ExtrinsicResponse.from_exception(
                raise_error=raise_error,
                error=error,
            )

    def get_metagraph_info(
        self,
        netuid: int,
        mechid: int = 0,
        block=None,
    ):
        del netuid
        del mechid
        del block


class MockMetagraph(bt.Metagraph):
    def __init__(self, netuid=1, network="mock", subtensor=None):
        super().__init__(netuid=netuid, network=network, sync=False)

        if subtensor is not None:
            self.subtensor = subtensor
        # Force lite=False: upstream bittensor 10.x MockSubtensor has an
        # unfixed bug where neuron_for_uid_lite() reads NeuronInfo.rank
        # (removed by PR #3214 / commit d1f5e50). The non-lite path goes
        # through neurons() -> neuron_for_uid() which does not touch the
        # missing attribute. Tracked upstream; drop this override once
        # opentensor/bittensor ships the fix.
        self.sync(subtensor=subtensor, lite=False)

        for axon in self.axons:
            axon.ip = "127.0.0.0"
            axon.port = 8091

        bt.logging.info("Mock metagraph initialized.")


class MockDendrite(bt.Dendrite):
    """
    Replaces a real bittensor network request with a mock request that just returns some static response for all axons that are passed and adds some random delay.
    """

    def __init__(self, wallet):
        with patch(
            "bittensor.utils.networking.get_external_ip",
            return_value=MOCK_AXON_IP,
        ):
            super().__init__(wallet)

    async def forward(
        self,
        axons: List[bt.Axon],
        synapse: bt.Synapse | None = None,
        timeout: float = 12,
        deserialize: bool = True,
        run_async: bool = True,
        streaming: bool = False,
    ):
        del run_async
        if streaming:
            raise NotImplementedError("Streaming not implemented yet.")
        if synapse is None:
            synapse = bt.Synapse()

        async def query_all_axons(streaming: bool):
            """Queries all axons for responses."""

            del streaming

            async def single_axon_response(i, axon):
                """Queries a single axon for a response."""

                del i
                start_time = time.time()
                s = synapse.copy()
                # Attach some more required data so it looks real
                s = self.preprocess_synapse_for_request(axon, s, timeout)
                # We just want to mock the response, so we'll just fill in some data
                process_time = random.random()
                if process_time < timeout:
                    s.dendrite.process_time = str(time.time() - start_time)
                    # Update the status code and status message of the dendrite to match the axon
                    # Mirror the scaffold miner behavior for mock-network tests.
                    if hasattr(s, "dummy_input") and hasattr(s, "dummy_output"):
                        s.dummy_output = s.dummy_input * 2
                    s.dendrite.status_code = 200
                    s.dendrite.status_message = "OK"
                    s.dendrite.process_time = str(process_time)
                else:
                    if hasattr(s, "dummy_output"):
                        s.dummy_output = 0
                    s.dendrite.status_code = 408
                    s.dendrite.status_message = "Timeout"
                    s.dendrite.process_time = str(timeout)

                # Return the updated synapse object after deserializing if requested
                if deserialize:
                    return s.deserialize()
                else:
                    return s

            return await asyncio.gather(
                *(
                    single_axon_response(i, target_axon)
                    for i, target_axon in enumerate(axons)
                )
            )

        return await query_all_axons(streaming)

    def __str__(self) -> str:
        """
        Returns a string representation of the Dendrite object.

        Returns:
            str: The string representation of the Dendrite object in the format "dendrite(<user_wallet_address>)".
        """
        return "MockDendrite({})".format(self.keypair.ss58_address)


class MockRuntimeProvider:
    def __init__(self) -> None:
        self._subtensor: MockSubtensor | None = None

    def create_base(self, config: bt.Config) -> BaseRuntimeComponents:
        wallet = get_mock_wallet()
        subtensor = MockSubtensor(config.netuid, wallet=wallet)
        self._subtensor = subtensor
        metagraph = MockMetagraph(config.netuid, subtensor=subtensor)
        return BaseRuntimeComponents(
            wallet=wallet,
            subtensor=subtensor,
            metagraph=metagraph,
        )

    def create_subtensor(self, config: bt.Config) -> bt.Subtensor:
        # The mock chain lives in-process and holds neuron registrations;
        # "reconnecting" must reuse it or check_registered would fail against
        # an empty fresh chain.
        if self._subtensor is not None:
            return self._subtensor
        return MockSubtensor(config.netuid)

    def create_miner_axon(self, wallet: bt.Wallet, config: bt.Config) -> bt.Axon:
        return build_mock_axon(wallet, config)

    def create_validator_dendrite(
        self, wallet: bt.Wallet, config: bt.Config
    ) -> bt.Dendrite:
        del config
        return MockDendrite(wallet=wallet)

    def create_validator_axon(self, wallet: bt.Wallet, config: bt.Config) -> bt.Axon:
        return build_mock_axon(wallet, config)

    def create_miner_dendrite(
        self, wallet: bt.Wallet, config: bt.Config
    ) -> bt.Dendrite:
        del config
        return MockDendrite(wallet=wallet)
