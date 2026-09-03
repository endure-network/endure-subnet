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

import argparse
import asyncio
import copy
import sys
import threading
import traceback
from abc import abstractmethod
from typing import Union

import bittensor as bt

from endure.base.neuron import BaseNeuron
from endure.base.rate_gate import ChainRpcRestartRequired, ChainRpcStalled
from endure.base.shutdown import join_thread_or_raise
from endure.runtime.types import RuntimeProvider
from endure.utils.config import add_miner_args
from endure.utils.logging import safe_endpoint_label, safe_error


class BaseMinerNeuron(BaseNeuron):
    """Bittensor transport lifecycle and request hooks for miners."""

    neuron_type: str = "MinerNeuron"

    @classmethod
    def add_args(cls, parser: argparse.ArgumentParser):
        super().add_args(parser)
        add_miner_args(cls, parser)

    def __init__(
        self,
        config=None,
        runtime_provider: RuntimeProvider | None = None,
    ):
        super().__init__(config=config, runtime_provider=runtime_provider)

        # Warn when operator configuration relaxes the normal admission policy.
        if not self.config.blacklist.force_validator_permit:
            bt.logging.warning(
                "You are allowing non-validators to send requests to your miner. This is a security risk."
            )
        if self.config.blacklist.allow_non_registered:
            bt.logging.warning(
                "You are allowing non-registered entities to send requests to your miner. This is a security risk."
            )
        # The axon exposes the miner's Bittensor request handlers.
        self.axon = self.runtime_provider.create_miner_axon(self.wallet, self.config)

        # Bind transport callbacks supplied by the concrete miner.
        bt.logging.info("Attaching forward function to miner axon.")
        self.axon.attach(
            forward_fn=self.forward,
            blacklist_fn=self.blacklist,
            priority_fn=self.priority,
        )
        bt.logging.info("Miner axon created.")

        self.should_exit: bool = False
        self.is_running: bool = False
        self.thread: Union[threading.Thread, None] = None
        self._shutdown_event = threading.Event()
        self._metagraph_lock = threading.Lock()
        self.lock = asyncio.Lock()

    @abstractmethod
    async def forward(self, synapse: bt.Synapse) -> bt.Synapse: ...

    @abstractmethod
    async def blacklist(self, synapse: bt.Synapse) -> tuple[bool, str]: ...

    @abstractmethod
    async def priority(self, synapse: bt.Synapse) -> float: ...

    def set_weights(self) -> None:
        """Miners do not emit validator weights."""

    def _due_for_sync(self, last_sync_block: int) -> bool:
        """Whether ``epoch_length`` blocks have elapsed since our last sync.

        Keyed on the miner's own last sync block rather than
        ``metagraph.last_update[uid]``: the latter only advances when this uid
        sets weights, which a miner never does, so it cannot pace a miner.
        """
        return (self.block - last_sync_block) >= self.config.neuron.epoch_length

    def run(self):
        """Serve the miner axon and keep its metagraph view synchronized."""

        try:
            # Check registration and advertise the configured axon.
            self.sync()
            # Name the endpoint the axon is actually served through: in mock
            # mode that is the in-process chain, not the configured default.
            bt.logging.info(
                f"Serving miner axon on network "
                f"{safe_endpoint_label(self.subtensor.chain_endpoint)} "
                f"with netuid {self.config.netuid}"
            )
            self.axon.serve(netuid=self.config.netuid, subtensor=self.subtensor)
            self.axon.start()
            bt.logging.info(f"Miner starting at block: {self.block}")
        except Exception as error:  # noqa: BLE001 - worker boundary must redact.
            bt.logging.error(f"Miner startup failed: {safe_error(error)}")
            bt.logging.debug(safe_error(traceback.format_exc()))
            self.should_exit = True
            return

        # This loop maintains the miner's operations until intentionally stopped.
        # Pace on blocks elapsed since our OWN last sync — not
        # metagraph.last_update[uid], which only advances when a neuron sets
        # weights. A miner never sets weights, so that value is frozen and the
        # old condition was permanently past-due, busy-syncing the chain.
        last_sync_block = self.block
        try:
            while not self.should_exit:
                try:
                    while not self._due_for_sync(last_sync_block):
                        # Wake immediately when process teardown requests exit.
                        self._shutdown_event.wait(1)

                        # Check if we should exit.
                        if self.should_exit:
                            break

                    # Check if we should exit.
                    if self.should_exit:
                        break

                    # Sync metagraph and potentially set weights.
                    self.sync()
                    last_sync_block = self.block
                    self.step += 1

                except ChainRpcRestartRequired as err:
                    bt.logging.error(f"chain RPC restart required: {safe_error(err)}")
                    self._chain_rpc_restart_required = True
                    self.should_exit = True
                    break
                except ChainRpcStalled as err:
                    bt.logging.error(f"chain RPC stalled: {safe_error(err)}")
                    self._reconnect_subtensor(reason=f"{err.operation_name} timeout")
                    self._shutdown_event.wait(1)
                # Unforeseen errors are logged per-iteration and the loop
                # continues: a transient sync/metagraph/chain failure must not
                # silently kill the miner service (mirrors the validator loop).
                # An explicit should_exit still ends it.
                except Exception as err:
                    bt.logging.error(f"Error during mining: {safe_error(err)}")
                    bt.logging.debug(safe_error(traceback.format_exc()))
                    if self.should_exit:
                        break
                    # A failed sync leaves last_sync_block stale, so the wait
                    # loop's sleep is skipped and the retry would otherwise
                    # hammer a dead endpoint in a hot loop — throttle it here.
                    self._shutdown_event.wait(1)

        # If someone intentionally stops the miner, it'll safely terminate operations.
        except KeyboardInterrupt:
            self.axon.stop()
            bt.logging.success("Miner killed by keyboard interrupt.")
            sys.exit(1)

    def run_in_background_thread(self):
        """Start the miner loop in a daemon thread."""
        if not self.is_running:
            bt.logging.debug("Starting miner in background thread.")
            self.should_exit = False
            self._shutdown_event.clear()
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()
            self.is_running = True
            bt.logging.debug("Started")

    def stop_run_thread(self):
        """Request shutdown and join the miner thread."""
        if self.is_running:
            bt.logging.debug("Stopping miner in background thread.")
            self.should_exit = True
            self._shutdown_event.set()
            transport_error: Exception | None = None
            try:
                self.axon.stop()
            except Exception as error:  # noqa: BLE001 - finish joining before surfacing.
                transport_error = error
            if self.thread is None:
                raise RuntimeError("is_running True but thread unset")
            join_thread_or_raise(self.thread, name="miner loop")
            self.thread = None
            self.is_running = False
            if transport_error is not None:
                raise RuntimeError("miner axon failed to stop") from transport_error
            bt.logging.debug("Stopped")

    def __enter__(self):
        """Start the background loop for context-manager use."""
        self.run_in_background_thread()
        return self

    def close_transport_resources(self) -> None:
        """Close non-serving network clients after every worker has stopped."""
        failures: list[Exception] = []
        dendrite = getattr(self, "dendrite", None)
        close_dendrite = getattr(dendrite, "close_session", None)
        if callable(close_dendrite):
            try:
                close_dendrite()
            except Exception as error:  # noqa: BLE001 - close every resource.
                failures.append(error)
        close_subtensor = getattr(self.subtensor, "close", None)
        if callable(close_subtensor):
            try:
                close_subtensor()
            except Exception as error:  # noqa: BLE001 - close every resource.
                failures.append(error)
        if failures:
            raise RuntimeError("miner transport cleanup incomplete") from failures[0]

    def __exit__(self, exc_type, exc_value, traceback):
        """Stop the background loop when leaving the context."""
        self.stop_run_thread()
        self.close_transport_resources()

    def resync_metagraph(self):
        """Refresh the miner's metagraph view."""
        bt.logging.info("resync_metagraph()")

        refreshed_metagraph = copy.deepcopy(self.metagraph)
        refreshed_metagraph.sync(subtensor=self.subtensor)
        with self._metagraph_lock:
            self.metagraph = refreshed_metagraph
            self.refresh_uid()
