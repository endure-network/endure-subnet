from __future__ import annotations

import os

import bittensor as bt

from endure.base.rate_gate import (
    DEFAULT_MESSAGE_BURST,
    DEFAULT_MESSAGE_RATE_PER_SECOND,
    RpcMessageLimiter,
    install_rpc_message_limiter,
)
from endure.runtime.types import BaseRuntimeComponents


def _env_positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 1 else default


class LiveRuntimeProvider:
    def __init__(self) -> None:
        # One limiter shared by the base connection and every reconnect, so the
        # provider's per-second budget is enforced across the whole process
        # rather than reset each time a websocket is rebuilt. Rate/burst are
        # env-tunable (ENDURE_RPC_MESSAGE_RATE/BURST) so ops can raise them once
        # the validator has its own independently metered credential.
        self._rpc_limiter = RpcMessageLimiter(
            rate_per_second=_env_positive_float(
                "ENDURE_RPC_MESSAGE_RATE", DEFAULT_MESSAGE_RATE_PER_SECOND
            ),
            burst=_env_positive_int("ENDURE_RPC_MESSAGE_BURST", DEFAULT_MESSAGE_BURST),
        )

    def create_base(self, config: bt.Config) -> BaseRuntimeComponents:
        wallet = bt.Wallet(config=config)
        subtensor = bt.Subtensor(config=config)
        install_rpc_message_limiter(subtensor, self._rpc_limiter)
        metagraph = subtensor.metagraph(config.netuid)
        return BaseRuntimeComponents(
            wallet=wallet,
            subtensor=subtensor,
            metagraph=metagraph,
        )

    def create_subtensor(self, config: bt.Config) -> bt.Subtensor:
        subtensor = bt.Subtensor(config=config)
        install_rpc_message_limiter(subtensor, self._rpc_limiter)
        return subtensor

    def create_miner_axon(self, wallet: bt.Wallet, config: bt.Config) -> bt.Axon:
        return bt.Axon(wallet=wallet, config=config)

    def create_validator_dendrite(
        self, wallet: bt.Wallet, config: bt.Config
    ) -> bt.Dendrite:
        del config
        return bt.Dendrite(wallet=wallet)

    def create_validator_axon(self, wallet: bt.Wallet, config: bt.Config) -> bt.Axon:
        return bt.Axon(wallet=wallet, config=config)

    def create_miner_dendrite(
        self, wallet: bt.Wallet, config: bt.Config
    ) -> bt.Dendrite:
        del config
        return bt.Dendrite(wallet=wallet)
