from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import bittensor as bt


@dataclass(slots=True)
class BaseRuntimeComponents:
    wallet: bt.Wallet
    subtensor: bt.Subtensor
    metagraph: bt.Metagraph


class RuntimeProvider(Protocol):
    def create_base(self, config: bt.Config) -> BaseRuntimeComponents: ...

    def create_subtensor(self, config: bt.Config) -> bt.Subtensor: ...

    def create_miner_axon(self, wallet: bt.Wallet, config: bt.Config) -> bt.Axon: ...

    def create_validator_dendrite(
        self, wallet: bt.Wallet, config: bt.Config
    ) -> bt.Dendrite: ...

    def create_validator_axon(
        self, wallet: bt.Wallet, config: bt.Config
    ) -> bt.Axon: ...

    def create_miner_dendrite(
        self, wallet: bt.Wallet, config: bt.Config
    ) -> bt.Dendrite: ...
