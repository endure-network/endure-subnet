from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import List
from unittest.mock import patch

import bittensor as bt
from bittensor.core.settings import version_as_int
from bittensor.core.types import ExtrinsicResponse
from bittensor.utils import networking
from bittensor_wallet.mock import get_mock_wallet

from endure.runtime.types import BaseRuntimeComponents

MOCK_AXON_IP = "127.0.0.1"
MOCK_BLOCK_SECONDS = 12


@dataclass(frozen=True, slots=True)
class MockMetagraphInfo:
    """The ``MetagraphInfo`` fields the emission plan selects."""

    netuid: int
    block: int
    owner_hotkey: str
    hotkeys: list[str]
    validator_permit: list[bool]
    last_update: list[int]
    weights_rate_limit: int


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

        # The mock validator holds a permit, as a staked validator would, so
        # emission planning can reach set_weights in scored mode.
        if wallet is not None:
            state = self.chain_state["SubtensorModule"]
            uid = self._get_most_recent_storage(
                state["Uids"][netuid][wallet.hotkey.ss58_address]
            )
            state["ValidatorPermit"][netuid][uid][self.block_number] = True

        # The mock chain advances on the wall clock like a real one, so epoch
        # pacing and the strict weights rate limit can come due.
        self._clock_origin = time.monotonic()
        self._clock_block = self.block_number
        # Every mock block is final at once.
        self.substrate.get_block_number = lambda _block_hash: self.get_current_block()

    def get_current_block(self) -> int:
        target = self._clock_block + int(
            (time.monotonic() - self._clock_origin) // MOCK_BLOCK_SECONDS
        )
        while self.block_number < target:
            self.do_block_step()
        return self.block_number

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

    def set_weights(
        self,
        wallet: bt.Wallet,
        netuid: int,
        uids: list[int],
        weights: list[int],
        version_key: int = version_as_int,
        **kwargs: object,
    ) -> ExtrinsicResponse:
        """Include a direct weight submission in the mock chain state."""
        del version_key
        del kwargs
        state = self.chain_state["SubtensorModule"]
        # Inclusion lands in the next block, strictly after the submission
        # block the validator prepared at, as on a real chain.
        block = self.get_current_block() + 1
        uid = self._get_most_recent_storage(
            state["Uids"][netuid][wallet.hotkey.ss58_address]
        )
        state["Weights"][netuid][uid][block] = list(zip(uids, weights, strict=True))
        state["LastUpdate"][netuid][uid][block] = block
        return ExtrinsicResponse(True, "Mock weights included")

    def neurons_lite(
        self, netuid: int, block: int | None = None
    ) -> list[bt.NeuronInfo]:
        # Upstream's lite path reads the removed NeuronInfo.rank (see
        # MockMetagraph.sync); the full records carry the same uid/hotkey.
        return self.neurons(netuid=netuid, block=block)

    def get_hyperparameter(
        self, param_name: str, netuid: int, block: int | None = None
    ) -> object:
        if param_name != "LastUpdate":
            return super().get_hyperparameter(
                param_name=param_name, netuid=netuid, block=block
            )
        return [int(neuron.last_update) for neuron in self.neurons(netuid, block)]

    def weights(
        self, netuid: int, mechid: int = 0, block: int | None = None
    ) -> list[tuple[int, list[tuple[int, int]]]]:
        del mechid
        state = self.chain_state["SubtensorModule"]["Weights"][netuid]
        return [
            (uid, list(self._get_most_recent_storage(state[uid], block) or []))
            for uid in sorted(state)
        ]

    def commit_reveal_enabled(self, netuid: int, block: int | None = None) -> bool:
        """The mock chain sets weights directly; it has no CR4 epoch state."""
        del netuid
        del block
        return False

    def get_metagraph_info(
        self,
        netuid: int,
        mechid: int = 0,
        selected_indices: list[int] | None = None,
        block: int | None = None,
    ) -> MockMetagraphInfo | None:
        """Serve the emission plan's selective snapshot from the mock chain.

        A full fetch (``Metagraph.sync``'s extra-info pass) stays ``None``: the
        mock chain cannot populate every ``MetagraphInfo`` field.
        """
        del mechid
        if selected_indices is None:
            return None
        at = self.get_current_block() if block is None else block
        neurons = self.neurons(netuid=netuid, block=at)
        owner = self._get_most_recent_storage(
            self.chain_state["SubtensorModule"]["SubnetOwner"].get(netuid, {}), at
        )
        return MockMetagraphInfo(
            netuid=netuid,
            block=at,
            owner_hotkey=str(owner or ""),
            hotkeys=[neuron.hotkey for neuron in neurons],
            validator_permit=[bool(neuron.validator_permit) for neuron in neurons],
            last_update=[int(neuron.last_update) for neuron in neurons],
            weights_rate_limit=int(self.weights_rate_limit(netuid=netuid)),
        )


class MockMetagraph(bt.Metagraph):
    def __init__(self, netuid=1, network="mock", subtensor=None):
        super().__init__(netuid=netuid, network=network, sync=False)

        if subtensor is not None:
            self.subtensor = subtensor
        self.sync(subtensor=subtensor)

        for axon in self.axons:
            axon.ip = "127.0.0.0"
            axon.port = 8091

        bt.logging.info("Mock metagraph initialized.")

    def sync(
        self,
        block: int | None = None,
        lite: bool | None = None,
        subtensor: bt.Subtensor | None = None,
    ) -> None:
        # Always lite=False, including the validator's periodic resync:
        # upstream bittensor 10.x MockSubtensor has an unfixed bug where
        # neuron_for_uid_lite() reads NeuronInfo.rank (removed by PR #3214 /
        # commit d1f5e50). The non-lite path goes through neurons() ->
        # neuron_for_uid(), which does not touch the missing attribute. Drop
        # this override once opentensor/bittensor ships the fix.
        del lite
        super().sync(block=block, lite=False, subtensor=subtensor)


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
