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
import os
import sys
import threading
import traceback
from abc import abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Sequence, Union

import bittensor as bt
import numpy as np
from bittensor.core.types import ExtrinsicResponse

from endure.base.neuron import BaseNeuron
from endure.base.rate_gate import (
    ChainRpcRestartRequired,
    ChainRpcStalled,
    GatedSubtensor,
    RateLimited,
)
from endure.base.shutdown import join_thread_or_raise
from endure.protocol.version_contract import CURRENT_VERSION_KEY
from endure.protocol.weight_intent import (
    WeightIntentPayload,
    canonical_weight_intent_hash,
)
from endure.runtime.types import RuntimeProvider
from endure.scoring.weight_processing import (
    chain_weight_vector,
    coerce_decimal,
    emission_candidate,
)
from endure.utils.config import add_validator_args
from endure.utils.logging import safe_endpoint_label, safe_error

ZERO = Decimal("0")

CONSECUTIVE_FAILURES_BEFORE_RECONNECT = 5
PROVIDER_THROTTLE_RECONNECT_THRESHOLD = 3
EMISSION_SUBMITTED = "submitted"
EMISSION_FAILED = "failed"
EMISSION_ERROR = "error"
WEIGHT_EMISSION_PERIOD_BLOCKS = 128
WEIGHT_EMISSION_FINALITY_MARGIN_BLOCKS = 12


@dataclass(frozen=True, slots=True)
class WeightEmissionAttempt:
    """Transport-level record of one set_weights attempt.

    Carries only low-level vectors and the SDK outcome — no storage or
    domain concepts — so subclasses can persist an audit trail without the
    base adapter learning about schemas (fairness-deltas spec §2).
    ``status`` is honest about finality: ``submitted`` means the SDK call
    returned success with ``wait_for_finalization=False``, not that the
    extrinsic finalized on-chain.
    """

    hotkeys: tuple[str, ...]
    raw_weights: tuple[Decimal, ...]
    processed_uids: tuple[int, ...]
    processed_weights: tuple[Decimal, ...]
    uint_uids: tuple[int, ...]
    uint_weights: tuple[int, ...]
    min_allowed_weights: int | None
    max_weight_limit: Decimal | None
    status: str
    block: int | None
    submission_block: int | None
    baseline_last_update_block: int | None
    period_blocks: int | None
    confirmation_state: str | None
    chain_identity: str | None = None
    netuid: int | None = None
    validator_uid: int | None = None
    validator_hotkey: str | None = None
    submission_mode: str | None = None
    intent_hash: str | None = None
    protocol_version_key: int | None = None
    commitment_hash: str | None = None
    reveal_round: int | None = None
    confirmation_deadline_block: int | None = None
    cr4_reveal_deadline_block: int | None = None


@dataclass(frozen=True, slots=True)
class WeightSubmissionResult:
    status: str
    confirmation_state: str
    submission_mode: str
    commitment_hash: str | None
    reveal_round: int | None
    confirmation_deadline_block: int | None
    cr4_reveal_deadline_block: int | None


@dataclass(frozen=True, slots=True)
class WeightIntentMetadata:
    baseline_last_update_block: int
    submission_block: int
    chain_identity: str
    netuid: int
    validator_uid: int
    validator_hotkey: str
    submission_mode: str
    intent_hash: str
    protocol_version_key: int
    cr4_reveal_deadline_block: int | None


def normalize_commitment_hash(value: str | bytes | bytearray) -> str:
    if isinstance(value, str):
        return value.removeprefix("0x").lower()
    return bytes(value).hex()


def _cr4_metadata(response: ExtrinsicResponse) -> tuple[str, int] | None:
    data = response.data
    if not isinstance(data, dict):
        return None
    commitment = data.get("commit_for_reveal")
    reveal_round = data.get("reveal_round")
    if not isinstance(commitment, (str, bytes, bytearray)):
        return None
    if not isinstance(reveal_round, int) or isinstance(reveal_round, bool):
        return None
    return normalize_commitment_hash(commitment), reveal_round


class BaseValidatorNeuron(BaseNeuron):
    """Bittensor transport lifecycle and execution hook for validators."""

    neuron_type: str = "ValidatorNeuron"

    @classmethod
    def add_args(cls, parser: argparse.ArgumentParser):
        super().add_args(parser)
        add_validator_args(cls, parser)

    def __init__(
        self,
        config=None,
        runtime_provider: RuntimeProvider | None = None,
    ):
        super().__init__(config=config, runtime_provider=runtime_provider)

        self._consecutive_loop_failures = 0
        self._chain_rpc_replacement_required_reason: str | None = None
        self._last_set_weights_ok: str | None = None
        self._consecutive_set_weights_failures = 0

        # Preserve UID-to-hotkey identity across metagraph refreshes.
        self.hotkeys = copy.deepcopy(self.metagraph.hotkeys)

        self.dendrite = self.runtime_provider.create_validator_dendrite(
            self.wallet, self.config
        )
        bt.logging.info("Validator dendrite created.")

        bt.logging.info("Building validation weights.")
        self.scores = [ZERO] * int(self.metagraph.n)

        # Restore the local lifecycle checkpoint before the first sync/save
        # cycle. Emission scores are reconstructed by the concrete validator
        # after its durable scoring storage exists.
        self.load_state()
        self._align_state_with_current_metagraph()

        self.sync()

        if not self.config.neuron.axon_off:
            self.serve_axon()
        else:
            bt.logging.warning("axon off, not serving ip to chain.")

        # Create the event loop lazily in the thread that calls run().
        self.loop: asyncio.AbstractEventLoop | None = None

        self.should_exit: bool = False
        self.is_running: bool = False
        self.thread: Union[threading.Thread, None] = None
        self._shutdown_event = threading.Event()
        self.lock = asyncio.Lock()

    def _align_state_with_current_metagraph(self) -> None:
        current_hotkeys = copy.deepcopy(self.metagraph.hotkeys)
        overlap = min(len(self.hotkeys), len(current_hotkeys), len(self.scores))
        for uid in range(overlap):
            if self.hotkeys[uid] != current_hotkeys[uid]:
                self.scores[uid] = ZERO

        if len(self.scores) != int(self.metagraph.n):
            new_scores = [ZERO] * int(self.metagraph.n)
            copy_len = min(len(self.scores), int(self.metagraph.n))
            new_scores[:copy_len] = self.scores[:copy_len]
            self.scores = new_scores

        self.hotkeys = current_hotkeys

    def serve_axon(self):
        """Serve axon to enable external connections."""

        bt.logging.info("serving ip to chain...")
        self.axon = self.runtime_provider.create_validator_axon(
            self.wallet, self.config
        )
        response = self.subtensor.serve_axon(
            netuid=self.config.netuid,
            axon=self.axon,
        )
        if not response.success:
            raise RuntimeError(f"Failed to serve Axon: {response.message}")
        bt.logging.info(
            f"Running validator on network "
            f"{safe_endpoint_label(self.config.subtensor.chain_endpoint)} "
            f"with netuid {self.config.netuid}"
        )

    @abstractmethod
    async def forward(self) -> None: ...

    async def concurrent_forward(self):
        coroutines = [
            self.forward() for _ in range(self.config.neuron.num_concurrent_forwards)
        ]
        await asyncio.gather(*coroutines)

    def run(self):
        """Run validator ticks, chain synchronization, and weight emission."""
        if not self._initial_sync():
            return

        bt.logging.info(f"Validator starting at block: {self._safe_block()}")

        # This loop maintains the validator's operations until intentionally stopped.
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            while True:
                try:
                    if not self._run_iteration():
                        break
                except ChainRpcRestartRequired as err:
                    bt.logging.error(f"chain RPC restart required: {safe_error(err)}")
                    self._chain_rpc_restart_required = True
                    break
                except ChainRpcStalled as err:
                    bt.logging.error(f"chain RPC stalled: {safe_error(err)}")
                    self._reconnect_subtensor(reason=f"{err.operation_name} timeout")
                    self._consecutive_loop_failures = 0
                except Exception as err:
                    if not self._handle_iteration_error(err):
                        break

        # If someone intentionally stops the validator, it'll safely terminate operations.
        except KeyboardInterrupt:
            self.axon.stop()
            bt.logging.success("Validator killed by keyboard interrupt.")
            sys.exit(1)
        finally:
            if self.loop is not None:
                if not self.loop.is_closed():
                    self.loop.close()
                self.loop = None
            asyncio.set_event_loop(None)

    def _initial_sync(self) -> bool:
        try:
            self.sync()
        except RateLimited:
            bt.logging.warning("initial validator sync deferred by RPC gate")
        except ChainRpcRestartRequired as error:
            bt.logging.error(f"initial chain RPC restart required: {safe_error(error)}")
            self._chain_rpc_restart_required = True
            return False
        except ChainRpcStalled as error:
            bt.logging.error(f"initial validator sync stalled: {safe_error(error)}")
            self._reconnect_subtensor(reason=f"{error.operation_name} timeout")
        except Exception as error:  # noqa: BLE001 - worker boundary must redact.
            bt.logging.error(f"Validator startup sync failed: {safe_error(error)}")
            bt.logging.debug(safe_error(traceback.format_exc()))
            self.should_exit = True
            return False
        return True

    def _run_iteration(self) -> bool:
        bt.logging.info(
            f"validator operation=forward phase=start step={self.step} "
            f"block={self._safe_block()}"
        )
        if self.loop is None:
            raise RuntimeError("validator event loop is unavailable")
        self.loop.run_until_complete(self.concurrent_forward())
        bt.logging.info(f"validator operation=forward phase=complete step={self.step}")
        if self.should_exit:
            return False
        bt.logging.info(f"validator operation=chain_sync phase=start step={self.step}")
        self.sync()
        bt.logging.info(
            f"validator operation=chain_sync phase=complete step={self.step}"
        )
        self._maybe_reconnect_on_provider_throttles()
        self.step += 1
        self._consecutive_loop_failures = 0
        return True

    def _handle_iteration_error(self, error: Exception) -> bool:
        bt.logging.error(f"Error during validation: {safe_error(error)}")
        bt.logging.debug(safe_error(traceback.format_exc()))
        if self.should_exit:
            return False
        self._consecutive_loop_failures += 1
        if self._consecutive_loop_failures >= CONSECUTIVE_FAILURES_BEFORE_RECONNECT:
            self._reconnect_subtensor(reason="consecutive loop failures")
            self._consecutive_loop_failures = 0
        return True

    def _maybe_reconnect_on_provider_throttles(self) -> None:
        # A wedged websocket can keep returning provider 429s that sync()
        # swallows as normal deferral, so the loop-failure reconnect never fires.
        # Rebuild the connection after a streak of real provider throttles to
        # shed a socket the provider has pinned.
        if self._consecutive_provider_throttles < PROVIDER_THROTTLE_RECONNECT_THRESHOLD:
            return
        bt.logging.warning(
            "rebuilding subtensor after "
            f"{self._consecutive_provider_throttles} consecutive provider throttles"
        )
        self._reconnect_subtensor(reason="consecutive provider throttles")
        self._consecutive_provider_throttles = 0

    def run_in_background_thread(self):
        """Start the validator loop in a daemon thread."""
        if not self.is_running:
            bt.logging.debug("Starting validator in background thread.")
            self.should_exit = False
            self._shutdown_event.clear()
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()
            self.is_running = True
            bt.logging.debug("Started")

    def stop_run_thread(self):
        """Request shutdown and join the validator thread."""
        if self.is_running:
            bt.logging.debug("Stopping validator in background thread.")
            self.should_exit = True
            self._shutdown_event.set()
            transport_error: Exception | None = None
            axon = getattr(self, "axon", None)
            if axon is not None:
                try:
                    axon.stop()
                except Exception as error:  # noqa: BLE001 - join before surfacing.
                    transport_error = error
            if self.thread is None:
                raise RuntimeError("is_running True but thread unset")
            join_thread_or_raise(self.thread, name="validator loop")
            self.thread = None
            self.is_running = False
            if transport_error is not None:
                raise RuntimeError("validator axon failed to stop") from transport_error
            bt.logging.debug("Stopped")

    def __enter__(self):
        self.run_in_background_thread()
        return self

    def close_transport_resources(self) -> None:
        """Close non-serving network clients after every worker has stopped."""
        failures: list[Exception] = []
        close_dendrite = getattr(self.dendrite, "close_session", None)
        if callable(close_dendrite):
            try:
                close_dendrite()
            except Exception as error:  # noqa: BLE001 - close every resource.
                failures.append(error)
        try:
            self.gated_subtensor.close()
        except Exception as error:  # noqa: BLE001 - close every resource.
            failures.append(error)
        if failures:
            raise RuntimeError("validator transport cleanup incomplete") from failures[
                0
            ]

    def __exit__(self, exc_type, exc_value, traceback):
        """Stop the background loop when leaving the context."""
        self.stop_run_thread()
        self.close_transport_resources()

    def set_weights(self) -> None:
        """Normalize the current score vector and submit it."""
        self._emit_weight_candidate(self.scores)

    def _emit_weight_candidate(self, weights: Sequence[Decimal]) -> None:
        """Execute one candidate through the shared durable emission hooks."""

        if self.config.neuron.disable_set_weights:
            return
        raw_weights = emission_candidate(weights)
        if raw_weights is None:
            bt.logging.warning(
                "no positive miner scores — abstaining from weight emission "
                "rather than emitting uniform or inverted weights"
            )
            return

        bt.logging.debug("raw_weights", raw_weights)
        bt.logging.debug("raw_weight_uids", str(self.metagraph.uids.tolist()))
        hotkeys = tuple(self.metagraph.hotkeys)
        processed_weight_uids: tuple[int, ...] = ()
        processed_weights: tuple[Decimal, ...] = ()
        uint_uids: tuple[int, ...] = ()
        uint_weights: tuple[int, ...] = ()
        min_allowed_weights: int | None = None
        max_weight_limit: Decimal | None = None
        status = EMISSION_ERROR
        confirmation_state: str | None = None
        prepared_batch_id: int | None = None
        preparation_completed = False
        intent: WeightIntentMetadata | None = None
        submission: WeightSubmissionResult | None = None
        try:
            # Fetched once, BEFORE processing, and passed both into the
            # watched chain-weight transform and the emission hook: the audit
            # trail must record the exact limits that produced the processed
            # vector — a later re-query could observe different chain state.
            min_allowed_weights = int(
                self.subtensor.min_allowed_weights(netuid=self.config.netuid)
            )
            max_weight_limit = coerce_decimal(
                self.subtensor.max_weight_limit(netuid=self.config.netuid)
            )
            vector = chain_weight_vector(
                raw_weights,
                uids=self.metagraph.uids,
                metagraph_size=int(self.metagraph.n),
                min_allowed_weights=min_allowed_weights,
                max_weight_limit=max_weight_limit,
            )
            processed_weight_uids, processed_weights, uint_uids, uint_weights = (
                vector.processed_uids,
                vector.processed_weights,
                vector.uint_uids,
                vector.uint_weights,
            )
            bt.logging.debug("chain weight vector", vector)

            # Submit without blocking this watchdog-protected tick for inclusion.
            # A later metagraph refresh confirms the submission for free.
            # This must be an uncached pre-submission boundary: a cached block
            # sampled after the SDK call can already be stale relative to an
            # earlier weight update and falsely confirm this attempt.
            intent = self._prepare_weight_intent(uint_uids, uint_weights)
            prepared_attempt = WeightEmissionAttempt(
                hotkeys=hotkeys,
                raw_weights=tuple(raw_weights),
                processed_uids=tuple(processed_weight_uids),
                processed_weights=tuple(processed_weights),
                uint_uids=tuple(uint_uids),
                uint_weights=tuple(uint_weights),
                min_allowed_weights=min_allowed_weights,
                max_weight_limit=max_weight_limit,
                status=EMISSION_ERROR,
                block=intent.submission_block,
                submission_block=intent.submission_block,
                baseline_last_update_block=intent.baseline_last_update_block,
                period_blocks=WEIGHT_EMISSION_PERIOD_BLOCKS,
                confirmation_state="prepared",
                chain_identity=intent.chain_identity,
                netuid=intent.netuid,
                validator_uid=intent.validator_uid,
                validator_hotkey=intent.validator_hotkey,
                submission_mode=intent.submission_mode,
                intent_hash=intent.intent_hash,
                protocol_version_key=intent.protocol_version_key,
                confirmation_deadline_block=(
                    intent.submission_block
                    + WEIGHT_EMISSION_PERIOD_BLOCKS
                    + WEIGHT_EMISSION_FINALITY_MARGIN_BLOCKS
                ),
                cr4_reveal_deadline_block=intent.cr4_reveal_deadline_block,
            )
            prepared_batch_id = self._on_weights_prepared(prepared_attempt)
            preparation_completed = True
            try:
                submission = self._submit_prepared_weights(prepared_attempt)
                status = submission.status
                confirmation_state = submission.confirmation_state
            except RateLimited:
                status = EMISSION_FAILED
                confirmation_state = "failed"
                raise
        finally:
            if status != EMISSION_SUBMITTED:
                self._record_weight_failure()
            attempt = WeightEmissionAttempt(
                hotkeys=hotkeys,
                raw_weights=tuple(raw_weights),
                processed_uids=tuple(processed_weight_uids),
                processed_weights=tuple(processed_weights),
                uint_uids=tuple(uint_uids),
                uint_weights=tuple(uint_weights),
                min_allowed_weights=min_allowed_weights,
                max_weight_limit=max_weight_limit,
                status=status,
                block=self._safe_block(),
                submission_block=(None if intent is None else intent.submission_block),
                baseline_last_update_block=(
                    None if intent is None else intent.baseline_last_update_block
                ),
                period_blocks=(
                    WEIGHT_EMISSION_PERIOD_BLOCKS if intent is not None else None
                ),
                confirmation_state=confirmation_state,
                chain_identity=(None if intent is None else intent.chain_identity),
                netuid=(None if intent is None else intent.netuid),
                validator_uid=(None if intent is None else intent.validator_uid),
                validator_hotkey=(None if intent is None else intent.validator_hotkey),
                submission_mode=(
                    submission.submission_mode
                    if submission is not None
                    else None
                    if intent is None
                    else intent.submission_mode
                ),
                intent_hash=(None if intent is None else intent.intent_hash),
                protocol_version_key=(
                    None if intent is None else intent.protocol_version_key
                ),
                commitment_hash=(
                    None if submission is None else submission.commitment_hash
                ),
                reveal_round=(None if submission is None else submission.reveal_round),
                confirmation_deadline_block=(
                    None
                    if submission is None
                    else submission.confirmation_deadline_block
                ),
                cr4_reveal_deadline_block=(
                    submission.cr4_reveal_deadline_block
                    if submission is not None
                    else None
                    if intent is None
                    else intent.cr4_reveal_deadline_block
                ),
            )
            if preparation_completed or intent is None:
                self._notify_weights_emitted(attempt, prepared_batch_id)
            self._replace_transport_if_required()

    def _replace_transport_if_required(self) -> None:
        reason = getattr(self, "_chain_rpc_replacement_required_reason", None)
        if reason is None:
            return
        self._chain_rpc_replacement_required_reason = None
        self._reconnect_subtensor(reason=reason)

    def _weight_submission_baseline(self) -> int:
        if not 0 <= self.uid < len(self.metagraph.last_update):
            raise RuntimeError(f"validator uid {self.uid} is outside the metagraph")
        hotkeys = self.metagraph.hotkeys
        if (
            self.uid >= len(hotkeys)
            or hotkeys[self.uid] != self.wallet.hotkey.ss58_address
        ):
            raise RuntimeError("validator uid does not match the wallet hotkey")
        return int(self.metagraph.last_update[self.uid])

    def _prepare_weight_intent(
        self, uint_uids: Sequence[int], uint_weights: Sequence[int]
    ) -> WeightIntentMetadata:
        baseline_last_update_block = self._weight_submission_baseline()
        submission_block = self._fresh_submission_block()
        netuid = int(self.config.netuid)
        validator_uid = int(self.uid)
        validator_hotkey = str(self.wallet.hotkey.ss58_address)
        gated_subtensor: GatedSubtensor = self.gated_subtensor
        chain_identity = gated_subtensor.get_block_hash(0)
        submission_mode = (
            "cr4" if gated_subtensor.commit_reveal_enabled(netuid=netuid) else "direct"
        )
        cr4_reveal_deadline = (
            gated_subtensor.cr4_reveal_deadline_at(
                netuid=netuid,
                block=submission_block,
                finality_margin_blocks=WEIGHT_EMISSION_FINALITY_MARGIN_BLOCKS,
            )
            if submission_mode == "cr4"
            else None
        )
        targets = tuple(
            (uid, str(self.metagraph.hotkeys[uid]), weight)
            for uid, weight in zip(uint_uids, uint_weights, strict=True)
        )
        return WeightIntentMetadata(
            baseline_last_update_block=baseline_last_update_block,
            submission_block=submission_block,
            chain_identity=chain_identity,
            netuid=netuid,
            validator_uid=validator_uid,
            validator_hotkey=validator_hotkey,
            submission_mode=submission_mode,
            intent_hash=canonical_weight_intent_hash(
                WeightIntentPayload(
                    protocol_version_key=CURRENT_VERSION_KEY,
                    chain_identity=chain_identity,
                    netuid=netuid,
                    validator_uid=validator_uid,
                    validator_hotkey=validator_hotkey,
                    targets=targets,
                )
            ),
            protocol_version_key=CURRENT_VERSION_KEY,
            cr4_reveal_deadline_block=cr4_reveal_deadline,
        )

    def _submit_prepared_weights(
        self, attempt: WeightEmissionAttempt
    ) -> WeightSubmissionResult:
        try:
            response: ExtrinsicResponse = self.subtensor.set_weights(
                wallet=self.wallet,
                netuid=self.config.netuid,
                uids=list(attempt.uint_uids),
                weights=list(attempt.uint_weights),
                wait_for_finalization=False,
                wait_for_inclusion=False,
                max_attempts=1,
                mev_protection=False,
                period=WEIGHT_EMISSION_PERIOD_BLOCKS,
                raise_error=True,
                version_key=CURRENT_VERSION_KEY,
            )
        except RateLimited as deferred:
            if not deferred.provider_limited:
                raise
            self._consecutive_provider_throttles += 1
            bt.logging.warning(
                "set_weights outcome is ambiguous after provider throttle"
            )
            confirmation_deadline, cr4_reveal_deadline = (
                self._post_submission_deadlines(attempt)
            )
            return WeightSubmissionResult(
                status=EMISSION_ERROR,
                confirmation_state="ambiguous",
                submission_mode=attempt.submission_mode or "direct",
                commitment_hash=None,
                reveal_round=None,
                confirmation_deadline_block=confirmation_deadline,
                cr4_reveal_deadline_block=cr4_reveal_deadline,
            )
        except ChainRpcRestartRequired:
            raise
        except ChainRpcStalled as error:
            bt.logging.error(
                f"set_weights outcome is ambiguous after transport timeout: "
                f"{error.operation_name}"
            )
            confirmation_deadline, cr4_reveal_deadline = (
                self._post_submission_deadlines(attempt)
            )
            result = WeightSubmissionResult(
                status=EMISSION_ERROR,
                confirmation_state="ambiguous",
                submission_mode=attempt.submission_mode or "direct",
                commitment_hash=None,
                reveal_round=None,
                confirmation_deadline_block=confirmation_deadline,
                cr4_reveal_deadline_block=cr4_reveal_deadline,
            )
            self._chain_rpc_replacement_required_reason = "set_weights timeout"
            return result
        except Exception as error:  # noqa: BLE001 — SDK error types vary by RPC backend
            bt.logging.error(
                "set_weights outcome is ambiguous; not retrying: "
                f"{type(error).__name__}"
            )
            confirmation_deadline, cr4_reveal_deadline = (
                self._post_submission_deadlines(attempt)
            )
            return WeightSubmissionResult(
                status=EMISSION_ERROR,
                confirmation_state="ambiguous",
                submission_mode=attempt.submission_mode or "direct",
                commitment_hash=None,
                reveal_round=None,
                confirmation_deadline_block=confirmation_deadline,
                cr4_reveal_deadline_block=cr4_reveal_deadline,
            )
        if not response.success:
            bt.logging.error(f"set_weights failed: {safe_error(response.message)}")
            return WeightSubmissionResult(
                status=EMISSION_FAILED,
                confirmation_state="failed",
                submission_mode=attempt.submission_mode or "direct",
                commitment_hash=None,
                reveal_round=None,
                confirmation_deadline_block=None,
                cr4_reveal_deadline_block=None,
            )
        confirmation_deadline, cr4_reveal_deadline = self._post_submission_deadlines(
            attempt
        )
        commitment = _cr4_metadata(response)
        if attempt.submission_mode == "cr4" and commitment is None:
            bt.logging.warning(
                "set_weights accepted without CR4 commitment metadata; "
                "keeping outcome ambiguous"
            )
            return WeightSubmissionResult(
                status=EMISSION_ERROR,
                confirmation_state="ambiguous",
                submission_mode="cr4",
                commitment_hash=None,
                reveal_round=None,
                confirmation_deadline_block=confirmation_deadline,
                cr4_reveal_deadline_block=cr4_reveal_deadline,
            )
        bt.logging.info(f"set_weights submitted: {response.message}")
        return WeightSubmissionResult(
            status=EMISSION_SUBMITTED,
            confirmation_state="submitted",
            submission_mode="cr4" if commitment is not None else "direct",
            commitment_hash=(None if commitment is None else commitment[0]),
            reveal_round=(None if commitment is None else commitment[1]),
            confirmation_deadline_block=confirmation_deadline,
            cr4_reveal_deadline_block=cr4_reveal_deadline,
        )

    def _fresh_submission_block(self) -> int:
        """Read an uncached chain head immediately before submitting weights."""
        return int(self.subtensor.get_current_block())

    def _post_submission_deadlines(
        self, attempt: WeightEmissionAttempt
    ) -> tuple[int | None, int | None]:
        fallback = attempt.confirmation_deadline_block
        reveal_deadline = attempt.cr4_reveal_deadline_block
        try:
            sampled_after_submission = self._fresh_submission_block()
        except Exception:  # noqa: BLE001 — unknown mortality stays open and degraded
            return fallback, reveal_deadline
        sampled_deadline = (
            sampled_after_submission
            + WEIGHT_EMISSION_PERIOD_BLOCKS
            + WEIGHT_EMISSION_FINALITY_MARGIN_BLOCKS
        )
        confirmation_deadline = (
            sampled_deadline
            if fallback is None
            else max(
                fallback,
                min(
                    sampled_deadline,
                    fallback
                    + WEIGHT_EMISSION_PERIOD_BLOCKS
                    - WEIGHT_EMISSION_FINALITY_MARGIN_BLOCKS,
                ),
            )
        )
        if attempt.submission_mode != "cr4":
            return confirmation_deadline, reveal_deadline
        try:
            refreshed_reveal_deadline = self.gated_subtensor.cr4_reveal_deadline_at(
                netuid=int(self.config.netuid),
                block=sampled_after_submission,
                finality_margin_blocks=WEIGHT_EMISSION_FINALITY_MARGIN_BLOCKS,
            )
        except Exception:  # noqa: BLE001 — the durable prepared deadline remains safe
            return confirmation_deadline, reveal_deadline
        return confirmation_deadline, max(
            reveal_deadline or refreshed_reveal_deadline,
            refreshed_reveal_deadline,
        )

    def _safe_block(self) -> int | None:
        try:
            return int(self.block)
        except Exception:  # noqa: BLE001 — a wedged chain must not mask the emission outcome
            return None

    def _record_weight_failure(self) -> None:
        """Track emission failures independently from connection-health failures."""
        self._consecutive_set_weights_failures = (
            getattr(self, "_consecutive_set_weights_failures", 0) + 1
        )

    def _on_metagraph_synced(self) -> None:
        """Hook for durable confirmation after a successful metagraph refresh."""

    def _notify_weights_emitted(
        self, attempt: WeightEmissionAttempt, batch_id: int | None
    ) -> None:
        try:
            self._on_weights_emitted(attempt, batch_id)
        except Exception as error:  # noqa: BLE001 — observability must never break emission
            bt.logging.error(f"weight emission hook failed: {safe_error(error)}")

    def _on_weights_prepared(self, attempt: WeightEmissionAttempt) -> int | None:
        """Persist a submission intent before invoking the SDK."""
        _ = attempt
        return None

    def _on_weights_emitted(
        self, attempt: WeightEmissionAttempt, batch_id: int | None
    ) -> None:
        """No-op seam; subclasses persist the audit trail (spec §2)."""
        _ = attempt
        _ = batch_id

    def resync_metagraph(self):
        """Resync the metagraph and align scores with their owning hotkeys."""
        bt.logging.info("resync_metagraph()")

        # Sync the metagraph.
        self.metagraph.sync(subtensor=self.subtensor)
        self.refresh_uid()

        # Align against the identity owning each score, not a snapshot of the
        # mutable metagraph. A previous sync may have updated the metagraph
        # before a fallible reconciliation hook failed. Repair the score vector
        # on every successful sync, before invoking that hook again.
        hotkeys = list(self.metagraph.hotkeys)
        scores = [ZERO] * int(self.metagraph.n)
        overlap = min(len(scores), len(hotkeys), len(self.scores), len(self.hotkeys))
        for uid in range(overlap):
            if self.hotkeys[uid] == hotkeys[uid]:
                scores[uid] = self.scores[uid]
        self.scores = scores
        self.hotkeys = hotkeys
        self._on_metagraph_synced()

    def save_state(self):
        """Saves the state of the validator to a file."""
        bt.logging.info("Saving validator state.")

        # Save the state of the validator to file.
        np.savez(
            self.config.neuron.full_path + "/state.npz",
            step=self.step,
            hotkeys=self.hotkeys,
        )

    def load_state(self):
        """Loads the state of the validator from a file."""
        bt.logging.info("Loading validator state.")

        state_path = Path(self.config.neuron.full_path) / "state.npz"
        if not state_path.exists():
            bt.logging.info(
                f"No validator state found at {state_path}; using in-memory defaults."
            )
            return

        # Load the state of the validator from file. A torn or garbage file
        # (crash mid-save, disk corruption) must not brick startup: quarantine
        # it and fall back to in-memory defaults rather than crash-looping the
        # validator forever. Losing one checkpoint's EMA is recoverable; a
        # neuron that can never boot is not. Only the live fields (step,
        # hotkeys) are parsed and validated; both assign only after the whole
        # file validated, so a partially valid file can never leave a mixed
        # loaded/default state. A legacy 'scores' key from a pre-key-26
        # checkpoint is IGNORED entirely — SQLite EMA state is the sole score
        # authority now, so even garbage legacy score bytes must not prevent
        # step/hotkeys from restoring.
        try:
            with np.load(state_path) as state:
                step = int(state["step"])
                hotkeys = state["hotkeys"].tolist()
        except Exception as error:  # noqa: BLE001 — resilience must not brick boot
            quarantine = state_path.with_name(f"{state_path.name}.corrupt")
            counter = 0
            while quarantine.exists():
                counter += 1
                quarantine = state_path.with_name(
                    f"{state_path.name}.corrupt-{counter}"
                )
            os.replace(state_path, quarantine)
            bt.logging.error(
                f"corrupt validator state moved to {quarantine}; "
                f"starting from in-memory defaults: {error}"
            )
            return
        self.step = step
        self.hotkeys = hotkeys
