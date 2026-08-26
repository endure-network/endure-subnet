from __future__ import annotations

import copy
import threading
from collections.abc import Callable
from dataclasses import dataclass
from unittest.mock import MagicMock

import bittensor as bt
import pytest

from endure.base.rate_gate import (
    AdaptiveRpcGate,
    GatedSubtensor,
    PacedSyncConnection,
    RateLimited,
    RpcLimiterInstallError,
    RpcMessageLimiter,
    RpcPriority,
    install_rpc_message_limiter,
)


@dataclass
class _Clock:
    now: float = 0.0
    sleeps: list[float] | None = None

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        if self.sleeps is None:
            self.sleeps = []
        self.sleeps.append(seconds)
        self.now += seconds


class TestAdaptiveRpcGate:
    def test_successful_calls_share_zero_burst_pacing(self) -> None:
        clock = _Clock()
        gate = AdaptiveRpcGate(clock=clock, sleeper=clock.sleep)

        first = gate.call(RpcPriority.METAGRAPH, lambda: "first")
        second = gate.call(RpcPriority.BEST_EFFORT, lambda: "paced")

        assert first == "first"
        assert second == "paced"
        assert clock.sleeps == [1.0]

    def test_rate_limit_cooldown_is_authoritative_for_essential_work(
        self,
    ) -> None:
        clock = _Clock()
        gate = AdaptiveRpcGate(clock=clock)
        essential_calls = 0

        def rejected() -> None:
            raise RuntimeError({"code": -32029, "retryAfter": 4})

        with pytest.raises(RateLimited):
            gate.call(RpcPriority.METAGRAPH, rejected)

        def essential() -> int:
            nonlocal essential_calls
            essential_calls += 1
            return 17

        with pytest.raises(RateLimited) as deferred:
            gate.call(RpcPriority.ESSENTIAL, essential)

        assert essential_calls == 0
        assert deferred.value.retry_after_monotonic == 4.0
        assert gate.snapshot().rate_limited_total == 1
        assert gate.snapshot().degraded is True
        clock.now = 4.0
        assert gate.call(RpcPriority.ESSENTIAL, essential) == 17

    def test_success_recovery_shortens_future_pacing_without_bursting(self) -> None:
        clock = _Clock()
        gate = AdaptiveRpcGate(clock=clock, sleeper=clock.sleep)
        initial_rate = gate.snapshot().adaptive_rate

        for _ in range(2):
            with pytest.raises(RateLimited):
                gate.call(
                    RpcPriority.METAGRAPH,
                    lambda: (_ for _ in ()).throw(RuntimeError("429 throttled")),
                )
            clock.now = gate.snapshot().retry_after_monotonic

        storm_rate = gate.snapshot().adaptive_rate
        gate.call(RpcPriority.METAGRAPH, lambda: None)
        recovered_rate = gate.snapshot().adaptive_rate
        gate.call(RpcPriority.METAGRAPH, lambda: None)
        second_recovered_rate = gate.snapshot().adaptive_rate

        assert storm_rate < initial_rate
        assert storm_rate < recovered_rate < initial_rate
        assert second_recovered_rate > recovered_rate
        assert clock.sleeps == [pytest.approx(1.0 / recovered_rate)]
        assert gate.snapshot().rate_limited_total == 2

    def test_repeated_throttles_keep_pacing_delay_bounded(self) -> None:
        clock = _Clock()
        gate = AdaptiveRpcGate(clock=clock)

        for _ in range(10):
            requested_at = clock.now
            with pytest.raises(RateLimited) as limited:
                gate.call(
                    RpcPriority.METAGRAPH,
                    lambda: (_ for _ in ()).throw(RuntimeError("429 throttled")),
                )
            delay = limited.value.retry_after_monotonic - requested_at
            assert 0 < delay <= 60
            clock.now = limited.value.retry_after_monotonic

        assert gate.snapshot().adaptive_rate > 0

    def test_block_number_containing_429_is_not_misclassified_as_rate_limit(
        self,
    ) -> None:
        # Given a non-rate-limit error whose message carries a block number.
        clock = _Clock()
        gate = AdaptiveRpcGate(clock=clock)

        def non_rate_limit_error() -> None:
            raise RuntimeError("storage read failed at block 76729429")

        # When the gate evaluates it.
        with pytest.raises(RuntimeError, match="storage read failed"):
            gate.call(RpcPriority.METAGRAPH, non_rate_limit_error)

        # Then the error propagates unclassified — no deferral, no AIMD penalty.
        assert gate.snapshot().rate_limited_total == 0
        assert gate.snapshot().degraded is False

    def test_pacing_sleep_does_not_block_essential_calls(self) -> None:
        sleep_entered = threading.Event()
        release_sleep = threading.Event()
        paced_finished = threading.Event()
        essential_finished = threading.Event()

        def blocking_sleep(_seconds: float) -> None:
            sleep_entered.set()
            release_sleep.wait(timeout=2)

        gate = AdaptiveRpcGate(sleeper=blocking_sleep)
        gate.call(RpcPriority.METAGRAPH, lambda: None)

        paced_thread = threading.Thread(
            target=lambda: (
                gate.call(RpcPriority.METAGRAPH, lambda: None),
                paced_finished.set(),
            )
        )
        essential_thread = threading.Thread(
            target=lambda: (
                gate.call(RpcPriority.ESSENTIAL, lambda: None),
                essential_finished.set(),
            )
        )
        paced_thread.start()
        assert sleep_entered.wait(timeout=1)
        essential_thread.start()

        essential_completed_without_waiting = essential_finished.wait(timeout=0.2)
        release_sleep.set()
        paced_thread.join(timeout=2)
        essential_thread.join(timeout=2)

        assert essential_completed_without_waiting
        assert paced_finished.is_set()
        assert not paced_thread.is_alive()
        assert not essential_thread.is_alive()

    def test_provider_retry_hint_is_bounded(self) -> None:
        clock = _Clock()
        gate = AdaptiveRpcGate(clock=clock)

        def rejected() -> None:
            raise RuntimeError(f"429 retry after {'9' * 400}")

        with pytest.raises(RateLimited) as limited:
            gate.call(RpcPriority.METAGRAPH, rejected)

        assert limited.value.retry_after_monotonic <= 60

    def test_rate_limit_cooldown_starts_when_failure_is_observed(self) -> None:
        clock = _Clock()
        gate = AdaptiveRpcGate(clock=clock)

        def delayed_rejection() -> None:
            clock.now = 10.0
            raise RuntimeError("429 retry after 4")

        with pytest.raises(RateLimited) as limited:
            gate.call(RpcPriority.METAGRAPH, delayed_rejection)

        assert limited.value.retry_after_monotonic == 14.0

    def test_late_success_does_not_move_a_newer_reservation_backward(self) -> None:
        clock = _Clock()
        gate = AdaptiveRpcGate(clock=clock, sleeper=clock.sleep)
        first_started = threading.Event()
        release_first = threading.Event()

        def first_operation() -> None:
            first_started.set()
            release_first.wait(timeout=2)

        first_thread = threading.Thread(
            target=lambda: gate.call(RpcPriority.METAGRAPH, first_operation)
        )
        first_thread.start()
        assert first_started.wait(timeout=1)

        clock.now = 1.0
        gate.call(RpcPriority.METAGRAPH, lambda: None)
        release_first.set()
        first_thread.join(timeout=2)
        assert not first_thread.is_alive()

        clock.now = 1.1
        gate.call(RpcPriority.METAGRAPH, lambda: None)

        assert clock.sleeps == [pytest.approx(0.9)]


class _Substrate:
    def get_chain_finalised_head(self) -> str:
        return "finalized-hash"

    def get_block_number(self, block_hash: str) -> int:
        assert block_hash == "finalized-hash"
        return 90

    def get_events(self, block_hash: str):
        if block_hash != "hash-89":
            return []
        return [
            {
                "event": {
                    "module_id": "SubtensorModule",
                    "event_id": "TimelockedWeightsRevealed",
                    "attributes": [1, "validator"],
                }
            }
        ]

    def query_map(
        self,
        *,
        module: str,
        storage_function: str,
        params: list[int],
        block_hash: str,
        page_size: int,
        max_results: int,
    ) -> MagicMock:
        assert module == "SubtensorModule"
        assert storage_function == "TimelockedWeightCommits"
        assert params == [1]
        assert block_hash == "hash-90"
        assert page_size == 100
        assert max_results == 101
        result = MagicMock()
        result.__iter__.return_value = iter(
            [
                ("epoch-1", [("validator", 81, "0x" + ("ab" * 311), 42)]),
                ("epoch-2", [("validator", 89, "beef", 43)]),
            ]
        )
        return result


class _Delegate(bt.Subtensor):
    def __init__(self) -> None:
        self.close_calls = 0
        self.substrate = _Substrate()

    def thread_name(self) -> str:
        return threading.current_thread().name

    def close(self) -> None:
        self.close_calls += 1

    def get_block_hash(self, block: int | None = None) -> str:
        return f"hash-{block}"

    def commit_reveal_enabled(self, *, netuid: int) -> bool:
        return netuid == 1

    def get_hyperparameter(
        self, *, param_name: str, netuid: int, block: int
    ) -> list[int]:
        assert (param_name, netuid, block) == ("LastUpdate", 1, 90)
        return [80]

    def weights(
        self, *, netuid: int, block: int
    ) -> list[tuple[int, list[tuple[int, int]]]]:
        assert (netuid, block) == (1, 90)
        return [(0, [(4, 65535)])]

    def neurons_lite(self, netuid: int, block: int):
        assert (netuid, block) == (1, 90)
        validator = MagicMock(uid=0, hotkey="validator")
        miner = MagicMock(uid=4, hotkey="miner")
        return [validator, miner]

    def get_timelocked_weight_commits(
        self, *, netuid: int, block: int
    ) -> list[tuple[str, int, str, int]]:
        assert (netuid, block) == (1, 90)
        return [("validator", 81, "abcd", 42)]

    def get_epoch_schedule_state(self, netuid: int, block: int) -> MagicMock:
        assert (netuid, block) == (1, 90)
        return MagicMock(
            last_epoch_block=0,
            pending_epoch_at=0,
            subnet_epoch_index=0,
            tempo=360,
            blocks_since_last_step=90,
            current_block=90,
        )

    def get_subnet_hyperparameters(self, netuid: int, block: int) -> MagicMock:
        assert (netuid, block) == (1, 90)
        return MagicMock(commit_reveal_period=1)


class _RecordingGate(AdaptiveRpcGate):
    def __init__(self) -> None:
        super().__init__()
        self.calls: dict[str, RpcPriority] = {}
        self._calls_lock = threading.Lock()

    def call[T](self, priority: RpcPriority, operation: Callable[[], T]) -> T:
        result = operation()
        with self._calls_lock:
            self.calls[str(result)] = priority
        return result


def test_gated_subtensor_priority_is_context_local_between_threads() -> None:
    gate = _RecordingGate()
    subtensor = GatedSubtensor(_Delegate(), gate)
    essential_entered = threading.Event()
    best_effort_entered = threading.Event()
    essential_called = threading.Event()
    essential_exited = threading.Event()

    def run_essential() -> None:
        with subtensor.priority(RpcPriority.ESSENTIAL):
            essential_entered.set()
            assert best_effort_entered.wait(timeout=1)
            subtensor.thread_name()
            essential_called.set()
        essential_exited.set()

    def run_best_effort() -> None:
        assert essential_entered.wait(timeout=1)
        with subtensor.priority(RpcPriority.BEST_EFFORT):
            best_effort_entered.set()
            assert essential_called.wait(timeout=1)
            assert essential_exited.wait(timeout=1)
            subtensor.thread_name()

    essential_thread = threading.Thread(target=run_essential, name="essential-thread")
    best_effort_thread = threading.Thread(
        target=run_best_effort, name="best-effort-thread"
    )
    essential_thread.start()
    best_effort_thread.start()
    essential_thread.join(timeout=2)
    best_effort_thread.join(timeout=2)

    assert not essential_thread.is_alive()
    assert not best_effort_thread.is_alive()
    assert gate.calls == {
        "essential-thread": RpcPriority.ESSENTIAL,
        "best-effort-thread": RpcPriority.BEST_EFFORT,
    }


def test_gated_subtensor_close_bypasses_provider_cooldown() -> None:
    clock = _Clock()
    gate = AdaptiveRpcGate(clock=clock)
    delegate = _Delegate()
    subtensor = GatedSubtensor(delegate, gate)

    with pytest.raises(RateLimited):
        gate.call(
            RpcPriority.METAGRAPH,
            lambda: (_ for _ in ()).throw(RuntimeError("429 retry after 4")),
        )

    subtensor.close()

    assert delegate.close_calls == 1


def test_gated_subtensor_reads_finalized_weight_evidence() -> None:
    subtensor = GatedSubtensor(_Delegate(), AdaptiveRpcGate())

    assert subtensor.get_block_hash(0) == "hash-0"
    assert subtensor.commit_reveal_enabled(netuid=1) is True
    assert subtensor.finalized_block() == 90
    assert subtensor.last_updates_at(netuid=1, block=90) == (80,)
    assert subtensor.weights_at(netuid=1, block=90) == ((0, ((4, 65535),)),)
    assert subtensor.hotkeys_at(netuid=1, block=90) == (
        (0, "validator"),
        (4, "miner"),
    )
    assert subtensor.timelocked_weight_commits_at(netuid=1, block=90) == (
        ("validator", 81, "0x" + ("ab" * 311), 42),
        ("validator", 89, "beef", 43),
    )
    assert subtensor.timelocked_weight_reveals_between(
        netuid=1,
        validator_hotkey="validator",
        start_block=88,
        end_block=90,
    ) == (89,)
    assert (
        subtensor.cr4_reveal_deadline_at(netuid=1, block=90, finality_margin_blocks=12)
        == 732
    )


def test_gated_subtensor_rejects_truncated_commitment_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delegate = _Delegate()
    result = MagicMock()
    result.__iter__.return_value = iter(
        [(f"epoch-{epoch}", []) for epoch in range(101)]
    )
    monkeypatch.setattr(delegate.substrate, "query_map", MagicMock(return_value=result))
    subtensor = GatedSubtensor(delegate, AdaptiveRpcGate())

    with pytest.raises(RuntimeError, match="commitment evidence limit"):
        subtensor.timelocked_weight_commits_at(netuid=1, block=90)


def test_gated_subtensor_accepts_complete_100_epoch_commitment_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delegate = _Delegate()
    result = MagicMock()
    records = [(f"epoch-{epoch}", []) for epoch in range(100)]
    result.__iter__.return_value = iter(records)
    monkeypatch.setattr(delegate.substrate, "query_map", MagicMock(return_value=result))
    subtensor = GatedSubtensor(delegate, AdaptiveRpcGate())

    assert subtensor.timelocked_weight_commits_at(netuid=1, block=90) == ()


def test_deepcopy_gated_subtensor_preserves_live_transport() -> None:
    gate = AdaptiveRpcGate()
    delegate = _Delegate()
    subtensor = GatedSubtensor(delegate, gate)

    cloned = copy.deepcopy(subtensor)

    assert isinstance(cloned, GatedSubtensor)
    assert cloned is not subtensor
    assert cloned._delegate is delegate
    assert cloned._gate is gate


def test_deepcopy_gated_subtensor_memoizes_shared_transport_state() -> None:
    gate = AdaptiveRpcGate()
    delegate = _Delegate()
    subtensor = GatedSubtensor(delegate, gate)
    memo: dict[int, GatedSubtensor | bt.Subtensor | AdaptiveRpcGate] = {}

    cloned_subtensor = copy.deepcopy(subtensor, memo)

    assert cloned_subtensor._delegate is delegate
    assert cloned_subtensor._gate is gate
    assert memo[id(delegate)] is delegate
    assert memo[id(gate)] is gate


class TestRpcMessageLimiter:
    def test_bursts_then_paces_under_the_ceiling(self) -> None:
        clock = _Clock()
        limiter = RpcMessageLimiter(
            rate_per_second=8.0, burst=6, clock=clock, sleeper=clock.sleep
        )

        times: list[float] = []
        for _ in range(28):
            limiter.acquire()
            times.append(clock.now)

        assert times[:6] == [0.0] * 6
        assert times[6] == pytest.approx(0.125)
        assert times[27] == pytest.approx(2.75)
        for start in times:
            in_window = [moment for moment in times if start <= moment < start + 1.0]
            assert len(in_window) <= 14

    def test_idle_refills_the_burst_allowance(self) -> None:
        clock = _Clock()
        limiter = RpcMessageLimiter(
            rate_per_second=8.0, burst=6, clock=clock, sleeper=clock.sleep
        )

        for _ in range(6):
            limiter.acquire()
        assert clock.sleeps is None

        clock.now += 100.0
        for _ in range(6):
            limiter.acquire()
        assert clock.sleeps is None

    def test_rejects_non_positive_configuration(self) -> None:
        with pytest.raises(ValueError):
            RpcMessageLimiter(rate_per_second=0.0)
        with pytest.raises(ValueError):
            RpcMessageLimiter(burst=0)

    def test_lock_is_released_before_sleeping(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def blocking_sleep(_seconds: float) -> None:
            entered.set()
            release.wait(timeout=2)

        limiter = RpcMessageLimiter(
            rate_per_second=1.0, burst=1, sleeper=blocking_sleep
        )
        limiter.acquire()
        waiter = threading.Thread(target=limiter.acquire)
        waiter.start()
        assert entered.wait(timeout=1)

        acquired = limiter._lock.acquire(timeout=1)
        assert acquired
        limiter._lock.release()
        release.set()
        waiter.join(timeout=2)
        assert not waiter.is_alive()


class _FakeWsConnection:
    def __init__(self, label: str) -> None:
        self.label = label
        self.sent: list[object] = []
        self.close_code: int | None = None
        self.closed = 0

    def send(self, *args: object, **kwargs: object) -> None:
        self.sent.append(args[0] if args else None)

    def recv(self, *args: object, **kwargs: object) -> str:
        return f"recv-{self.label}"

    def close(self) -> None:
        self.closed += 1


class _FakeSubstrate:
    def __init__(self) -> None:
        self.connections: list[_FakeWsConnection] = [_FakeWsConnection("conn-1")]
        self.ws: object = self.connections[0]

    def connect(self, init: bool = False) -> object:
        if init or getattr(self.ws, "close_code", None):
            conn = _FakeWsConnection(f"conn-{len(self.connections) + 1}")
            self.connections.append(conn)
            self.ws = conn
        return self.ws


class _FakeSubtensor(bt.Subtensor):
    def __init__(self) -> None:
        self.substrate = _FakeSubstrate()


class _NoSubstrateSubtensor(bt.Subtensor):
    def __init__(self) -> None:
        pass


def test_paced_connection_spends_a_permit_and_delegates() -> None:
    permits = {"count": 0}

    class _CountingLimiter(RpcMessageLimiter):
        def acquire(self) -> None:
            permits["count"] += 1

    underlying = _FakeWsConnection("c")
    paced = PacedSyncConnection(underlying, _CountingLimiter())

    paced.send("frame")

    assert permits["count"] == 1
    assert underlying.sent == ["frame"]
    assert paced.recv() == "recv-c"


def test_paced_send_propagates_errors_without_retry() -> None:
    class _Boom(_FakeWsConnection):
        def send(self, *args: object, **kwargs: object) -> None:
            self.sent.append("attempt")
            raise RuntimeError("429 throttled")

    underlying = _Boom("boom")
    paced = PacedSyncConnection(underlying, RpcMessageLimiter())

    with pytest.raises(RuntimeError, match="429"):
        paced.send("x")
    assert underlying.sent == ["attempt"]


def test_install_paces_and_delegates_sends() -> None:
    clock = _Clock()
    limiter = RpcMessageLimiter(
        rate_per_second=8.0, burst=1, clock=clock, sleeper=clock.sleep
    )
    subtensor = _FakeSubtensor()

    install_rpc_message_limiter(subtensor, limiter)

    assert isinstance(subtensor.substrate.ws, PacedSyncConnection)
    ws = subtensor.substrate.connect()
    assert isinstance(ws, PacedSyncConnection)

    ws.send("first")
    ws.send("second")

    assert clock.sleeps == [pytest.approx(0.125)]
    assert subtensor.substrate.connections[-1].sent == ["first", "second"]


def test_install_rewraps_reconnected_connection() -> None:
    clock = _Clock()
    limiter = RpcMessageLimiter(
        rate_per_second=8.0, burst=64, clock=clock, sleeper=clock.sleep
    )
    subtensor = _FakeSubtensor()
    install_rpc_message_limiter(subtensor, limiter)

    first = subtensor.substrate.connect()
    assert isinstance(first, PacedSyncConnection)
    assert len(subtensor.substrate.connections) == 1

    subtensor.substrate.connections[0].close_code = 1006
    reconnected = subtensor.substrate.connect()

    assert isinstance(reconnected, PacedSyncConnection)
    assert isinstance(subtensor.substrate.ws, PacedSyncConnection)
    assert len(subtensor.substrate.connections) == 2

    reconnected.send("after-reconnect")
    assert subtensor.substrate.connections[1].sent == ["after-reconnect"]
    assert subtensor.substrate.connections[0].sent == []


def test_install_is_idempotent() -> None:
    subtensor = _FakeSubtensor()
    limiter = RpcMessageLimiter()

    install_rpc_message_limiter(subtensor, limiter)
    connect_after_first = subtensor.substrate.connect
    install_rpc_message_limiter(subtensor, limiter)

    assert subtensor.substrate.connect is connect_after_first


def test_install_raises_when_transport_surface_missing() -> None:
    with pytest.raises(RpcLimiterInstallError):
        install_rpc_message_limiter(_NoSubstrateSubtensor(), RpcMessageLimiter())


def test_paced_connection_rejects_a_non_sendable_object() -> None:
    with pytest.raises(RpcLimiterInstallError):
        PacedSyncConnection(object(), RpcMessageLimiter())
