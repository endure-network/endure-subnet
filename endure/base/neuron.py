# The MIT License (MIT)
# Copyright © 2023 Yuma Rao

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

import copy
import sys
from abc import ABC, abstractmethod

import bittensor as bt

from endure import __spec_version__ as spec_version
from endure.base.rate_gate import (
    AdaptiveRpcGate,
    GatedSubtensor,
    RateLimited,
    RpcPriority,
)
from endure.runtime.live import LiveRuntimeProvider
from endure.runtime.types import RuntimeProvider

# Sync calls set weights and also resyncs the metagraph.
from endure.utils.config import add_args, check_config, config
from endure.utils.logging import safe_endpoint_label, startup_config_summary
from endure.utils.misc import ttl_get_block


class BaseNeuron(ABC):
    """Shared Bittensor transport lifecycle for miners and validators."""

    neuron_type: str = "BaseNeuron"

    @classmethod
    def check_config(cls, config: "bt.Config") -> None:
        check_config(cls, config)

    @classmethod
    def add_args(cls, parser) -> None:
        add_args(cls, parser)

    @classmethod
    def build_config(cls) -> "bt.Config":
        return config(cls)

    config: "bt.Config"
    subtensor: "bt.Subtensor"
    gated_subtensor: GatedSubtensor
    rpc_gate: AdaptiveRpcGate
    wallet: "bt.Wallet"
    metagraph: "bt.Metagraph"
    runtime_provider: RuntimeProvider
    spec_version: int = spec_version

    @property
    def block(self):
        with self.gated_subtensor.priority(RpcPriority.ESSENTIAL):
            return ttl_get_block(self)

    def __init__(
        self,
        config: "bt.Config | None" = None,
        runtime_provider: RuntimeProvider | None = None,
    ) -> None:
        supplied_config = copy.deepcopy(config) if config is not None else None
        self.config = type(self).build_config()
        if supplied_config is not None:
            self.config.merge(supplied_config)
        self.check_config(self.config)

        bt.logging.set_config(config=self.config.logging)

        bt.logging.info(
            f"Startup configuration: "
            f"{startup_config_summary(self.config, neuron_type=self.neuron_type)}"
        )

        bt.logging.info("Setting up bittensor objects.")

        self.runtime_provider = runtime_provider or LiveRuntimeProvider()
        components = self.runtime_provider.create_base(self.config)
        self.wallet = components.wallet
        self.rpc_gate = AdaptiveRpcGate()
        self.gated_subtensor = GatedSubtensor(components.subtensor, self.rpc_gate)
        self.subtensor = self.gated_subtensor
        self.metagraph = components.metagraph

        bt.logging.info("Bittensor objects initialized.")

        self.check_registered()

        self.uid = self.metagraph.hotkeys.index(self.wallet.hotkey.ss58_address)
        bt.logging.info(
            f"Running neuron on subnet {self.config.netuid} with uid {self.uid} "
            f"using network {safe_endpoint_label(self.subtensor.chain_endpoint)}"
        )
        self.step = 0
        # Sync/weights pacing is keyed on our OWN last action blocks, not on
        # metagraph.last_update[uid]: last_update only advances when this uid
        # lands weights on chain, so it freezes for miners and for abstaining
        # validators, making last_update-based pacing permanently past-due
        # (full metagraph resync + weight attempt every tick). None means
        # "never checked" — the first check seeds it lazily so construction
        # never reads the chain for pacing.
        self._last_metagraph_sync: int | None = None
        self._last_weights_attempt: int | None = None
        self._consecutive_provider_throttles = 0

    @abstractmethod
    def run(self) -> None: ...

    @abstractmethod
    def resync_metagraph(self) -> None: ...

    @abstractmethod
    def set_weights(self) -> None: ...

    def sync(self):
        """Run the next due metagraph or weight action and persist local state."""
        provider_throttles_before_sync = self._consecutive_provider_throttles
        try:
            self.check_registered()

            sync_due = self.should_sync_metagraph()
            weights_due = self.should_set_weights()
            # A resync may fan out into many SDK reads; never put its burst and an
            # extrinsic in the same tick. The emission remains due for the next pass.
            if sync_due:
                with self.gated_subtensor.priority(RpcPriority.METAGRAPH):
                    self.resync_metagraph()
                self._last_metagraph_sync = self.block
            elif weights_due:
                with self.gated_subtensor.priority(RpcPriority.ESSENTIAL):
                    self.set_weights()
                self._last_weights_attempt = self.block
            # check_registered() above always exercises the connection, so a
            # completed sync without a later throttle proves the socket recovered.
            if self._consecutive_provider_throttles == provider_throttles_before_sync:
                self._consecutive_provider_throttles = 0
        except RateLimited as deferred:
            if deferred.provider_limited:
                self._consecutive_provider_throttles += 1
            bt.logging.warning(
                f"chain RPC deferred until {deferred.retry_after_monotonic}"
            )
        finally:
            # A deferred RPC is normal control flow, never a reason to skip a checkpoint.
            self.save_state()

    def check_registered(self):
        """Exit when the configured hotkey is not registered on this subnet."""
        with self.gated_subtensor.priority(RpcPriority.ESSENTIAL):
            registered = self.subtensor.is_hotkey_registered(
                netuid=self.config.netuid,
                hotkey_ss58=self.wallet.hotkey.ss58_address,
            )
        if not registered:
            bt.logging.error(
                f"Configured hotkey is not registered on netuid {self.config.netuid}. "
                "Register it with `btcli subnets register` before starting the neuron."
            )
            sys.exit(1)

    def should_sync_metagraph(self):
        """
        Check if enough epoch blocks have elapsed since our own last sync.

        The first check seeds the pacing from the current block and reports
        not-due: the metagraph was freshly built at construction, so the first
        resync is owed one epoch later, not immediately.
        """
        if self._last_metagraph_sync is None:
            self._last_metagraph_sync = self.block
            return False
        return (
            self.block - self._last_metagraph_sync
        ) > self.config.neuron.epoch_length

    def should_set_weights(self) -> bool:
        # Don't set weights on initialization.
        if self.step == 0:
            return False

        if self.config.neuron.disable_set_weights:
            return False

        if self.neuron_type == "MinerNeuron":  # miners never set weights
            return False

        if not self.rpc_gate.ready():
            return False

        # Pace on our own last attempt (seeded on first check), mirroring
        # should_sync_metagraph.
        if self._last_weights_attempt is None:
            self._last_weights_attempt = self.block
            return False
        return (
            self.block - self._last_weights_attempt
        ) > self.config.neuron.epoch_length

    def save_state(self):
        bt.logging.trace("no local lifecycle state configured for this neuron")

    def load_state(self):
        bt.logging.trace("no local lifecycle state configured for this neuron")
