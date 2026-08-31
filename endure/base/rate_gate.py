"""Provider-neutral adaptive control for synchronous chain RPC calls."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FuturesTimeout
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypedDict, runtime_checkable

import bittensor as bt

from endure.base.cr4_schedule import Cr4EpochSchedule, cr4_reveal_deadline_block

_MIN_ADAPTIVE_RATE = 1.0 / 60.0
_MAX_RETRY_AFTER_SECONDS = 60.0
_CR4_REVEAL_EVENT_ATTRIBUTE_COUNT = 2
_MAX_COMMITMENT_EPOCHS = 100
# Generous per-operation ceiling: a legitimate gated operation fans out to at
# most tens of paced websocket frames (~2/s), so anything beyond this is a
# stalled transport, not slow work. Observed in soak 2026-08-30: one hung
# chain RPC froze the validator loop for 25 minutes until the watchdog killed
# the process; a bounded call instead surfaces a failure the loop's
# consecutive-failure reconnect machinery can recover from.
_DEFAULT_OPERATION_TIMEOUT_SECONDS = 90.0
_MAX_ABANDONED_GENERATIONS = 3


@runtime_checkable
class _Closable(Protocol):
    def close(self) -> None: ...


@runtime_checkable
class _FinalizedSubstrate(Protocol):
    def get_chain_finalised_head(self) -> str: ...

    def get_block_number(self, block_hash: str) -> int: ...

    def query_map(
        self,
        *,
        module: str,
        storage_function: str,
        params: list[int],
        block_hash: str,
        page_size: int,
        max_results: int,
    ) -> _TimelockedCommitQuery: ...

    def get_events(self, block_hash: str) -> list[_EventRecord]: ...


class _EventPayload(TypedDict, total=False):
    module_id: str
    event_id: str
    attributes: list[int | str] | dict[str, int | str]


class _EventRecord(_EventPayload, total=False):
    event: _EventPayload


class _TimelockedCommitQuery(Protocol):
    def __iter__(
        self,
    ) -> Iterator[tuple[str, list[tuple[str, int, str, int]]]]: ...


class _NeuronIdentity(Protocol):
    uid: int
    hotkey: str


class _EpochSchedule(Protocol):
    last_epoch_block: int
    pending_epoch_at: int
    subnet_epoch_index: int
    tempo: int
    blocks_since_last_step: int
    current_block: int


class _SubnetHyperparameters(Protocol):
    commit_reveal_period: int


@runtime_checkable
class _Cr4ScheduleSubtensor(Protocol):
    def get_epoch_schedule_state(self, netuid: int, block: int) -> _EpochSchedule: ...

    def get_subnet_hyperparameters(
        self, netuid: int, block: int
    ) -> _SubnetHyperparameters: ...


@runtime_checkable
class _WeightEvidenceSubtensor(Protocol):
    substrate: _FinalizedSubstrate

    def get_block_hash(self, block: int | None = None) -> str: ...

    def commit_reveal_enabled(self, *, netuid: int) -> bool: ...

    def get_hyperparameter(
        self, *, param_name: str, netuid: int, block: int
    ) -> list[int] | tuple[int, ...] | None: ...

    def weights(
        self, *, netuid: int, block: int
    ) -> list[tuple[int, list[tuple[int, int]]]]: ...

    def neurons_lite(self, netuid: int, block: int) -> list[_NeuronIdentity]: ...

    def get_timelocked_weight_commits(
        self, *, netuid: int, block: int
    ) -> list[tuple[str, int, str, int]]: ...


class MissingCloseOperation(TypeError):
    pass


class MissingWeightEvidenceOperations(TypeError):
    pass


class MissingCr4ScheduleOperations(TypeError):
    pass


class RpcGenerationAlreadyBound(RuntimeError):
    pass


class WeightEvidenceLimitExceeded(RuntimeError):
    pass


class RpcPriority(StrEnum):
    """Work classes ordered by validator liveness importance."""

    ESSENTIAL = "essential"
    METAGRAPH = "metagraph"
    BEST_EFFORT = "best_effort"


@dataclass(slots=True)
class RateLimited(Exception):
    """Typed scheduled-work signal produced from a gateway throttle response."""

    retry_after_monotonic: float
    provider_limited: bool

    def __str__(self) -> str:
        return f"chain RPC deferred until monotonic {self.retry_after_monotonic}"


@dataclass(slots=True)
class ChainRpcStalled(Exception):
    """A transport generation exceeded its deadline and was poisoned."""

    operation_name: str
    timeout_seconds: float

    def __str__(self) -> str:
        return (
            f"chain RPC operation {self.operation_name!r} exceeded "
            f"{self.timeout_seconds}s; transport generation was poisoned"
        )


@dataclass(slots=True)
class ChainRpcRestartRequired(RuntimeError):
    """The process exhausted its bounded allowance for abandoned workers."""

    abandoned_generations: int

    def __str__(self) -> str:
        return (
            "chain RPC abandoned-generation capacity reached "
            f"({self.abandoned_generations}); process restart required"
        )


@dataclass(frozen=True, slots=True)
class RateGateSnapshot:
    """Small operator-facing view of the gate's current adaptive state."""

    adaptive_rate: float
    degraded: bool
    retry_after_monotonic: float
    rate_limited_total: int
    deferred_total: int
    abandoned_generations: int


class _AbandonedGenerationState:
    """Mutable count shared by every replacement transport generation."""

    def __init__(self) -> None:
        self.count = 0
        self.condition = threading.Condition()


class AdaptiveRpcGate:
    """AIMD scheduler whose mutable fields are the live process-wide budget."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        operation_timeout_seconds: float = _DEFAULT_OPERATION_TIMEOUT_SECONDS,
        _abandoned_state: _AbandonedGenerationState | None = None,
    ) -> None:
        if operation_timeout_seconds <= 0:
            raise ValueError("operation_timeout_seconds must be positive")
        self._clock = clock
        self._sleeper = sleeper
        self._operation_timeout_seconds = operation_timeout_seconds
        self._initial_rate = 1.0
        self._adaptive_rate = self._initial_rate
        self._next_request_monotonic = 0.0
        self._retry_after_monotonic = 0.0
        self._rate_limited_total = 0
        self._deferred_total = 0
        self._lock = threading.Lock()
        self._executor = self._new_executor()
        self._generation_lock = threading.Lock()
        self._transport_close: Callable[[], None] | None = None
        self._transport_identity: int | None = None
        self._poisoned_operation_name: str | None = None
        self._abandoned_state = _abandoned_state or _AbandonedGenerationState()

    @staticmethod
    def _new_executor() -> ThreadPoolExecutor:
        return ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="chain-rpc-generation"
        )

    def bind_transport_close(
        self, close: Callable[[], None], *, transport_identity: int | None = None
    ) -> None:
        """Bind the websocket close operation owned by this worker generation."""
        with self._generation_lock:
            if self._transport_close is not None:
                same_transport = (
                    transport_identity is not None
                    and self._transport_identity == transport_identity
                ) or (transport_identity is None and self._transport_close == close)
                if same_transport:
                    return
                raise RpcGenerationAlreadyBound
            self._transport_close = close
            self._transport_identity = transport_identity

    def replacement(self) -> AdaptiveRpcGate:
        """Create a fresh worker generation sharing the abandonment budget."""
        replacement = type(self)(
            clock=self._clock,
            sleeper=self._sleeper,
            operation_timeout_seconds=self._operation_timeout_seconds,
            _abandoned_state=self._abandoned_state,
        )
        with self._lock:
            replacement._adaptive_rate = self._adaptive_rate
            replacement._next_request_monotonic = self._next_request_monotonic
            replacement._retry_after_monotonic = self._retry_after_monotonic
            replacement._rate_limited_total = self._rate_limited_total
            replacement._deferred_total = self._deferred_total
        return replacement

    def call[T](
        self,
        priority: RpcPriority,
        operation: Callable[[], T],
        *,
        operation_name: str = "chain_rpc",
    ) -> T:
        """Run one operation or raise a non-blocking scheduled-work signal."""
        reservation_monotonic: float | None = None
        while True:
            now = self._clock()
            with self._lock:
                if now < self._retry_after_monotonic:
                    self._deferred_total += 1
                    raise RateLimited(
                        retry_after_monotonic=self._retry_after_monotonic,
                        provider_limited=False,
                    )
                if priority is RpcPriority.ESSENTIAL:
                    break
                pacing_delay = self._next_request_monotonic - now
                if pacing_delay <= 0:
                    self._next_request_monotonic = now + (1.0 / self._adaptive_rate)
                    reservation_monotonic = self._next_request_monotonic
                    break
            self._sleeper(pacing_delay)
        try:
            result = self._call_bounded(operation, operation_name=operation_name)
        except Exception as error:
            if not _is_rate_limited(error):
                raise
            failure_observed_at = self._clock()
            with self._lock:
                self._record_rate_limit(error, failure_observed_at)
                retry_after_monotonic = self._retry_after_monotonic
            raise RateLimited(
                retry_after_monotonic=retry_after_monotonic,
                provider_limited=True,
            ) from error
        if priority is not RpcPriority.ESSENTIAL:
            success_observed_at = self._clock()
            with self._lock:
                self._record_success(success_observed_at, reservation_monotonic)
        return result

    def _call_bounded[T](self, operation: Callable[[], T], *, operation_name: str) -> T:
        # The lock gives each caller a full deadline after the prior serialized
        # operation completes. The executor and websocket have identical
        # lifetimes: timeout poisons both before any later caller can submit.
        with self._generation_lock:
            with self._abandoned_state.condition:
                if self._abandoned_state.count >= _MAX_ABANDONED_GENERATIONS:
                    raise ChainRpcRestartRequired(self._abandoned_state.count)
            if self._poisoned_operation_name is not None:
                raise ChainRpcStalled(
                    operation_name=self._poisoned_operation_name,
                    timeout_seconds=self._operation_timeout_seconds,
                )
            future = self._executor.submit(operation)
            try:
                return future.result(timeout=self._operation_timeout_seconds)
            except _FuturesTimeout:
                self._poisoned_operation_name = operation_name
                future.cancel()
                self._executor.shutdown(wait=False, cancel_futures=True)
                if not future.done():
                    with self._abandoned_state.condition:
                        self._abandoned_state.count += 1
                    future.add_done_callback(self._release_abandoned_generation)
                close = self._transport_close
                if close is not None:
                    threading.Thread(
                        target=self._close_poisoned_transport,
                        args=(close,),
                        name="chain-rpc-poison-close",
                        daemon=True,
                    ).start()
                raise ChainRpcStalled(
                    operation_name=operation_name,
                    timeout_seconds=self._operation_timeout_seconds,
                ) from None

    @staticmethod
    def _close_poisoned_transport(close: Callable[[], None]) -> None:
        try:
            close()
        except Exception as error:  # noqa: BLE001 - timeout remains authoritative
            bt.logging.debug(
                f"chain RPC poisoned transport close failed: {type(error).__name__}"
            )

    def _release_abandoned_generation[T](self, _future: Future[T]) -> None:
        with self._abandoned_state.condition:
            self._abandoned_state.count -= 1
            self._abandoned_state.condition.notify_all()

    def close_generation(self) -> None:
        """Reject new work, close the transport, and retire the worker."""
        with self._generation_lock:
            if self._poisoned_operation_name is None:
                self._poisoned_operation_name = "closed"
                close = self._transport_close
                if close is not None:
                    close()
                self._executor.shutdown(wait=False, cancel_futures=True)

    def snapshot(self) -> RateGateSnapshot:
        """Return a stable health payload without exposing mutable gate state."""
        now = self._clock()
        with self._lock:
            with self._abandoned_state.condition:
                abandoned_generations = self._abandoned_state.count
            return RateGateSnapshot(
                adaptive_rate=self._adaptive_rate,
                degraded=now < self._retry_after_monotonic,
                retry_after_monotonic=self._retry_after_monotonic,
                rate_limited_total=self._rate_limited_total,
                deferred_total=self._deferred_total,
                abandoned_generations=abandoned_generations,
            )

    def ready(self) -> bool:
        """Report whether deferred non-essential work may be scheduled now."""
        now = self._clock()
        with self._lock:
            return now >= self._retry_after_monotonic

    def _record_rate_limit(self, error: Exception, now: float) -> None:
        self._rate_limited_total += 1
        self._adaptive_rate = max(_MIN_ADAPTIVE_RATE, self._adaptive_rate * 0.5)
        hinted_delay = _retry_after_seconds(error)
        delay = hinted_delay if hinted_delay is not None else 1.0 / self._adaptive_rate
        self._retry_after_monotonic = max(self._retry_after_monotonic, now + delay)
        self._next_request_monotonic = max(
            self._next_request_monotonic, self._retry_after_monotonic
        )

    def _record_success(self, now: float, reservation_monotonic: float | None) -> None:
        if now < self._retry_after_monotonic:
            return
        self._adaptive_rate = min(
            self._initial_rate,
            self._adaptive_rate + (self._initial_rate / 8),
        )
        recovered_reservation = now + (1.0 / self._adaptive_rate)
        if self._next_request_monotonic == reservation_monotonic:
            self._next_request_monotonic = recovered_reservation
        else:
            self._next_request_monotonic = max(
                self._next_request_monotonic,
                recovered_reservation,
            )


def _is_rate_limited(error: Exception) -> bool:
    message = " ".join(str(argument) for argument in error.args)
    if "-32029" in message:
        return True
    # Digit-boundary guard: a bare "429" substring matches block numbers
    # (e.g. 76729429) and epoch-timestamp fractions. Learned 07-19/07-20.
    return bool(re.search(r"(?<!\d)(?<!\d\.)429(?!\d)", message))


def _retry_after_seconds(error: Exception) -> float | None:
    message = " ".join(str(argument) for argument in error.args)
    match = re.search(r"retry[_ -]?after[^0-9]*([0-9]+(?:\.[0-9]+)?)", message, re.I)
    if match is None:
        return None
    return min(float(match.group(1)), _MAX_RETRY_AFTER_SECONDS)


class GatedSubtensor(bt.Subtensor):
    """Facade that routes SDK and metagraph-initiated calls through one gate."""

    def __init__(self, delegate: bt.Subtensor, gate: AdaptiveRpcGate) -> None:
        self._delegate = delegate
        self._gate = gate
        close = getattr(delegate, "close", None)
        if not callable(close):
            raise MissingCloseOperation

        def close_transport() -> None:
            close()

        self._gate.bind_transport_close(
            close_transport, transport_identity=id(delegate)
        )
        self._priority = ContextVar(
            f"rpc_priority_{id(self)}", default=RpcPriority.METAGRAPH
        )

    @contextmanager
    def priority(self, priority: RpcPriority):
        """Temporarily classify all delegate calls made by a validator operation."""
        token = self._priority.set(priority)
        try:
            yield
        finally:
            self._priority.reset(token)

    def close(self) -> None:
        """Close the live transport without scheduling it as chain work."""
        self._gate.close_generation()

    def get_block_hash(self, block: int | None = None) -> str:
        delegate = self._weight_evidence_delegate()
        return str(
            self._gate.call(
                self._priority.get(),
                lambda: delegate.get_block_hash(block),
                operation_name="get_block_hash",
            )
        )

    def commit_reveal_enabled(self, *, netuid: int) -> bool:
        delegate = self._weight_evidence_delegate()
        return bool(
            self._gate.call(
                self._priority.get(),
                lambda: delegate.commit_reveal_enabled(netuid=netuid),
                operation_name="commit_reveal_enabled",
            )
        )

    def finalized_block(self) -> int:
        delegate = self._weight_evidence_delegate()
        return int(
            self._gate.call(
                self._priority.get(),
                lambda: delegate.substrate.get_block_number(
                    delegate.substrate.get_chain_finalised_head()
                ),
                operation_name="finalized_block",
            )
        )

    def last_updates_at(self, *, netuid: int, block: int) -> tuple[int, ...]:
        delegate = self._weight_evidence_delegate()
        values = self._gate.call(
            self._priority.get(),
            lambda: delegate.get_hyperparameter(
                param_name="LastUpdate", netuid=netuid, block=block
            ),
            operation_name="last_updates_at",
        )
        if not isinstance(values, (list, tuple)):
            raise TypeError("LastUpdate must be a sequence")
        return tuple(int(value) for value in values)

    def weights_at(
        self, *, netuid: int, block: int
    ) -> tuple[tuple[int, tuple[tuple[int, int], ...]], ...]:
        delegate = self._weight_evidence_delegate()
        values = self._gate.call(
            self._priority.get(),
            lambda: delegate.weights(netuid=netuid, block=block),
            operation_name="weights_at",
        )
        return tuple(
            (int(uid), tuple((int(target), int(weight)) for target, weight in row))
            for uid, row in values
        )

    def hotkeys_at(self, *, netuid: int, block: int) -> tuple[tuple[int, str], ...]:
        delegate = self._weight_evidence_delegate()
        neurons = self._gate.call(
            self._priority.get(),
            lambda: delegate.neurons_lite(netuid=netuid, block=block),
            operation_name="hotkeys_at",
        )
        return tuple(
            sorted((int(neuron.uid), str(neuron.hotkey)) for neuron in neurons)
        )

    def timelocked_weight_commits_at(
        self, *, netuid: int, block: int
    ) -> tuple[tuple[str, int, str, int], ...]:
        delegate = self._weight_evidence_delegate()
        records = tuple(
            self._gate.call(
                self._priority.get(),
                lambda: delegate.substrate.query_map(
                    module="SubtensorModule",
                    storage_function="TimelockedWeightCommits",
                    params=[netuid],
                    block_hash=delegate.get_block_hash(block),
                    page_size=_MAX_COMMITMENT_EPOCHS,
                    max_results=_MAX_COMMITMENT_EPOCHS + 1,
                ),
                operation_name="timelocked_weight_commits_at",
            )
        )
        if len(records) > _MAX_COMMITMENT_EPOCHS:
            raise WeightEvidenceLimitExceeded("commitment evidence limit reached")
        return tuple(
            (str(hotkey), int(commit_block), str(commitment), int(reveal_round))
            for _epoch, commits in records
            for hotkey, commit_block, commitment, reveal_round in commits
        )

    def timelocked_weight_reveals_between(
        self, *, netuid: int, validator_hotkey: str, start_block: int, end_block: int
    ) -> tuple[int, ...]:
        """Return finalized CR4 reveal blocks for one validator and subnet."""
        delegate = self._weight_evidence_delegate()
        revealed: list[int] = []
        for block in range(start_block, end_block + 1):
            events = self._gate.call(
                self._priority.get(),
                lambda block=block: delegate.substrate.get_events(
                    delegate.get_block_hash(block)
                ),
                operation_name="timelocked_weight_reveals_between",
            )
            for record in events:
                event = record.get("event", record)
                if (
                    event.get("module_id") != "SubtensorModule"
                    or event.get("event_id") != "TimelockedWeightsRevealed"
                ):
                    continue
                attributes = event.get("attributes", [])
                if isinstance(attributes, dict):
                    event_netuid = attributes.get("netuid")
                    who = attributes.get("who")
                elif len(attributes) >= _CR4_REVEAL_EVENT_ATTRIBUTE_COUNT:
                    event_netuid, who = attributes[:2]
                else:
                    continue
                if event_netuid is None or who is None:
                    continue
                if int(event_netuid) == netuid and str(who) == validator_hotkey:
                    revealed.append(block)
        return tuple(revealed)

    def cr4_reveal_deadline_at(
        self, *, netuid: int, block: int, finality_margin_blocks: int
    ) -> int:
        delegate = self._delegate
        if not isinstance(delegate, _Cr4ScheduleSubtensor):
            raise MissingCr4ScheduleOperations
        schedule = self._gate.call(
            self._priority.get(),
            lambda: delegate.get_epoch_schedule_state(netuid, block),
            operation_name="get_epoch_schedule_state",
        )
        hyperparameters = self._gate.call(
            self._priority.get(),
            lambda: delegate.get_subnet_hyperparameters(netuid, block),
            operation_name="get_subnet_hyperparameters",
        )
        return cr4_reveal_deadline_block(
            Cr4EpochSchedule(
                last_epoch_block=int(schedule.last_epoch_block),
                pending_epoch_at=int(schedule.pending_epoch_at),
                subnet_epoch_index=int(schedule.subnet_epoch_index),
                tempo=int(schedule.tempo),
                blocks_since_last_step=int(schedule.blocks_since_last_step),
                current_block=int(schedule.current_block),
            ),
            reveal_period_epochs=int(hyperparameters.commit_reveal_period),
            finality_margin_blocks=finality_margin_blocks,
        )

    def _weight_evidence_delegate(self) -> _WeightEvidenceSubtensor:
        delegate = self._delegate
        if not isinstance(delegate, _WeightEvidenceSubtensor):
            raise MissingWeightEvidenceOperations
        return delegate

    def __deepcopy__(
        self,
        memo: dict[int, GatedSubtensor | bt.Subtensor | AdaptiveRpcGate],
    ) -> GatedSubtensor:
        memo[id(self._delegate)] = self._delegate
        memo[id(self._gate)] = self._gate
        clone = type(self)(self._delegate, self._gate)
        memo[id(self)] = clone
        return clone

    def __getattribute__(self, name: str):
        if name in {
            "_delegate",
            "_gate",
            "_priority",
            "priority",
            "close",
            "get_block_hash",
            "commit_reveal_enabled",
            "finalized_block",
            "last_updates_at",
            "weights_at",
            "hotkeys_at",
            "timelocked_weight_commits_at",
            "timelocked_weight_reveals_between",
            "cr4_reveal_deadline_at",
            "_weight_evidence_delegate",
            "__class__",
            "__deepcopy__",
        }:
            return super().__getattribute__(name)
        attribute = getattr(super().__getattribute__("_delegate"), name)
        if not callable(attribute):
            return attribute

        def invoke(*args, **kwargs):
            return (
                super(GatedSubtensor, self)
                .__getattribute__("_gate")
                .call(
                    super(GatedSubtensor, self).__getattribute__("_priority").get(),
                    lambda: attribute(*args, **kwargs),
                    operation_name=name,
                )
            )

        return invoke


# Conservative default for a provider credential SHARED by several processes
# (this deployment runs a validator plus miners on one endpoint): burst 2 +
# 2/s keeps a single process near ~3 frames in any second, so even six
# co-resident processes stay under a 20/s provider burst ceiling. Raise these
# (e.g. 8/6) only when the validator has its own independently metered
# credential. Overridable per process via env in the runtime provider.
DEFAULT_MESSAGE_RATE_PER_SECOND = 2.0
DEFAULT_MESSAGE_BURST = 2


class RpcLimiterInstallError(RuntimeError):
    """The substrate transport surface needed for message pacing is absent."""


class RpcMessageLimiter:
    """Wire-message shaper that spreads a chain-RPC burst under a provider cap.

    ``AdaptiveRpcGate`` paces top-level SDK operations, but a single
    ``set_weights`` or ``metagraph.sync`` fans out into 15-28 substrate JSON-RPC
    messages beneath that facade — the burst that trips a provider's per-second
    limit while total volume stays trivial. This limiter throttles every
    outbound websocket frame with a GCRA token bucket, so the burst drips out
    under the ceiling. It is deliberately priority-blind: the ESSENTIAL emission
    path is exactly the burst that must be shaped.
    """

    def __init__(
        self,
        *,
        rate_per_second: float = DEFAULT_MESSAGE_RATE_PER_SECOND,
        burst: int = DEFAULT_MESSAGE_BURST,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        if burst < 1:
            raise ValueError("burst must be at least 1")
        self._interval = 1.0 / rate_per_second
        self._burst_slack = self._interval * (burst - 1)
        self._clock = clock
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._next_arrival: float | None = None

    def acquire(self) -> None:
        """Block until one message may be sent, reserving its slot atomically."""
        with self._lock:
            now = self._clock()
            arrival = now if self._next_arrival is None else self._next_arrival
            wait = (arrival - self._burst_slack) - now
            self._next_arrival = (arrival if arrival > now else now) + self._interval
        # Sleep outside the lock: a waiting sender must never stall reservation
        # bookkeeping for other threads (mirrors AdaptiveRpcGate).
        if wait > 0:
            self._sleeper(wait)


class PacedSyncConnection:
    """Wraps a sync websocket connection so each ``send`` spends one limiter
    permit; every other attribute delegates to the wrapped connection."""

    def __init__(self, connection: object, limiter: RpcMessageLimiter) -> None:
        sender = getattr(connection, "send", None)
        if not callable(sender):
            raise RpcLimiterInstallError("wrapped connection has no send()")
        self._connection = connection
        self._send = sender
        self._limiter = limiter

    def send(self, *args: object, **kwargs: object) -> object:
        self._limiter.acquire()
        return self._send(*args, **kwargs)

    def __getattr__(self, name: str) -> object:
        return getattr(self._connection, name)


def install_rpc_message_limiter(
    subtensor: bt.Subtensor, limiter: RpcMessageLimiter
) -> None:
    """Route every outbound websocket frame of ``subtensor`` through ``limiter``.

    Wraps the live connection and decorates ``substrate.connect`` so pacing
    survives reconnects — every SDK send re-fetches the connection through
    ``connect()``, so a paced return value covers all send paths. Idempotent per
    substrate. Raises ``RpcLimiterInstallError`` if the installed
    async-substrate-interface surface is missing, so a validator fails loudly
    rather than silently emitting an unpaced burst. The surface is duck-typed,
    not isinstance-checked, so real connections and test doubles are both paced.
    """
    substrate = getattr(subtensor, "substrate", None)
    connection = getattr(substrate, "ws", None)
    original_connect = getattr(substrate, "connect", None)
    if substrate is None or connection is None or not callable(original_connect):
        raise RpcLimiterInstallError(
            "expected subtensor.substrate with .ws and .connect(); chain RPC "
            "message pacing cannot be installed (async-substrate-interface 2.2.1)"
        )
    if isinstance(connection, PacedSyncConnection):
        return

    def pace(conn: object) -> PacedSyncConnection:
        if isinstance(conn, PacedSyncConnection):
            return conn
        return PacedSyncConnection(conn, limiter)

    def paced_connect(init: bool = False) -> PacedSyncConnection:
        paced = pace(original_connect(init=init))
        substrate.ws = paced
        return paced

    substrate.ws = pace(connection)
    substrate.connect = paced_connect
