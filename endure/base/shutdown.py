"""Process-level shutdown signalling shared by the neuron entrypoints."""

from __future__ import annotations

import atexit
import os
import signal
import sys
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from types import FrameType
from typing import NoReturn, Protocol

import bittensor as bt

SHUTDOWN_SIGNALS: tuple[signal.Signals, ...] = (signal.SIGINT, signal.SIGTERM)
# Dendrite submissions legitimately wait up to twelve seconds. Leave enough
# room for one in-flight operation to settle before declaring teardown wedged.
SHUTDOWN_JOIN_TIMEOUT_SECONDS = 30.0


def join_thread_or_raise(
    thread: threading.Thread,
    *,
    name: str,
    timeout_seconds: float = SHUTDOWN_JOIN_TIMEOUT_SECONDS,
) -> None:
    """Join one worker and fail visibly rather than claiming a false stop."""
    thread.join(timeout_seconds)
    if thread.is_alive():
        raise RuntimeError(f"{name} did not stop within {timeout_seconds:g} seconds")


def install_shutdown_handlers() -> threading.Event:
    """Route SIGINT and SIGTERM to one stop event; must run on the main thread.

    A neuron launched as a shell background job (the Makefile dev targets, most
    supervisors) inherits SIGINT ignored, and Python keeps an inherited SIG_IGN,
    so Ctrl+C or ``kill -INT`` would otherwise never reach the process at all.
    Installing explicit handlers restores delivery and gives both signals the
    same graceful stop instead of SIGTERM's abrupt default.
    """
    stop = threading.Event()

    def _request_stop(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        stop.set()

    for signum in SHUTDOWN_SIGNALS:
        signal.signal(signum, _request_stop)
    return stop


def _exit_status(code: object) -> int:
    """Mirror the interpreter's SystemExit status conversion."""
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    print(code, file=sys.stderr)
    return 1


def run_entrypoint(main: Callable[[], None], *, grace_seconds: float) -> NoReturn:
    """Run a neuron's main, then end the process without interpreter finalization.

    Finalization joins non-daemon threads and runs SDK ``__del__`` teardown: an
    abandoned archive worker, or an unclosed SyncSubstrate whose ``__del__``
    joins its websocket thread, can block there forever. Once finalization has
    started, daemon threads can no longer run, so no timer can rescue it; and
    the kernel drops default-action signals such as SIGALRM sent to a PID
    namespace's init, which Python is in a container started without an init.
    Exit callbacks, which drain the log queues, run first while threads are
    still alive; a daemon timer bounds them.
    """
    try:
        main()
        code = 0
    except SystemExit as error:
        code = _exit_status(error.code)
    except BaseException:  # noqa: BLE001 — the process boundary must still exit
        traceback.print_exc()
        code = 1
    terminate_process(code, grace_seconds=grace_seconds)


def terminate_process(code: int, *, grace_seconds: float) -> NoReturn:
    """Drain exit callbacks under a bounded timer, then ``os._exit`` at once."""
    timer = threading.Timer(grace_seconds, os._exit, args=(code,))
    timer.daemon = True
    timer.start()
    # os._exit skips registered drains, so run them first. The hook is private
    # CPython API: if a toolchain drops it, exit without the drain rather than
    # turning a clean exit into a traceback.
    run_exitfuncs = getattr(atexit, "_run_exitfuncs", None)
    if callable(run_exitfuncs):
        run_exitfuncs()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
    os._exit(code)


class StartupShutdownGuard:
    """End the process if a shutdown signal arrives before the run loop starts.

    The signal handlers only set ``stop``, which nothing polls while the neuron
    is still being constructed; a wedged chain connect or metagraph fetch would
    otherwise survive SIGTERM/SIGINT indefinitely. Construction that finishes
    within ``grace_seconds`` of the signal hands off to the run loop's normal
    shutdown instead.
    """

    def __init__(self, stop: threading.Event, *, grace_seconds: float) -> None:
        self._started = threading.Event()
        watcher = threading.Thread(
            target=self._watch,
            args=(stop, grace_seconds),
            name="startup-shutdown-guard",
            daemon=True,
        )
        watcher.start()

    def started(self) -> None:
        """Construction finished; the run loop now owns shutdown."""
        self._started.set()

    def _watch(self, stop: threading.Event, grace_seconds: float) -> None:
        stop.wait()
        if self._started.wait(grace_seconds):
            return
        print(
            "shutdown requested during startup; exiting before construction ends",
            file=sys.stderr,
            flush=True,
        )
        terminate_process(1, grace_seconds=grace_seconds)


# Bounds both a watchdog-triggered teardown and the exit-callback drain at the
# finalization-free entrypoint boundary.
WATCHDOG_TEARDOWN_GRACE_SECONDS = 60
# Within Docker's 45 s stop grace: a signal during construction waits this long
# for construction to finish before the startup guard ends the process.
STARTUP_SHUTDOWN_GRACE_SECONDS = 10
_WATCHDOG_POLL_SECONDS = 5


def schedule_forced_exit_after_grace(
    grace_seconds: float = WATCHDOG_TEARDOWN_GRACE_SECONDS,
) -> threading.Timer:
    """Bound a watchdog-triggered teardown that may join a wedged worker.

    SystemExit only reaches the finalization-free entrypoint boundary after the
    neuron's ``with`` teardown joins its workers, and whatever tripped the
    watchdog may have left one wedged. A daemon timer bounds that teardown
    while it still runs with threads alive.
    """
    timer = threading.Timer(grace_seconds, os._exit, args=(1,))
    timer.daemon = True
    timer.start()
    return timer


class ChainRpcRestartLatch(Protocol):
    """A neuron whose abandoned chain RPC workers can demand a hard exit."""

    def chain_rpc_restart_required(self) -> bool: ...


class ProcessTerminator(Protocol):
    def __call__(self, code: int, *, grace_seconds: float) -> NoReturn: ...


@dataclass(frozen=True, slots=True)
class NeuronLifecycle[N: ChainRpcRestartLatch]:
    """One neuron entrypoint's startup guard, watchdog loop and restart latch.

    Each neuron builds this inside ``main`` from its own module-level seams,
    so a test that patches one neuron's ``terminate_process`` or grace
    constant affects only that neuron.
    """

    name: str
    terminate: ProcessTerminator
    schedule_forced_exit: Callable[[], object]
    startup_grace_seconds: float
    teardown_grace_seconds: float

    def construct(self, stop: threading.Event, build: Callable[[], N]) -> N:
        """Build the neuron under a ``StartupShutdownGuard``, then hand off."""
        startup = StartupShutdownGuard(stop, grace_seconds=self.startup_grace_seconds)
        neuron = build()
        startup.started()
        return neuron

    def force_restart_if_rpc_abandoned(self, neuron: N) -> None:
        """Hard-exit once the neuron's chain RPC restart latch has tripped.

        A normal exit would join the abandoned non-daemon RPC workers at
        interpreter shutdown and could hang forever.
        """
        if neuron.chain_rpc_restart_required() is not True:
            return
        bt.logging.error(
            f"{self.name} forcing process restart after chain RPC "
            "abandonment capacity was reached"
        )
        # Drain the log queues first: the abandoned non-daemon RPC workers
        # would hang a normal interpreter shutdown, and a raw os._exit loses
        # this line.
        self.terminate(1, grace_seconds=self.teardown_grace_seconds)

    def watch(
        self,
        stop: threading.Event,
        neuron: N,
        *,
        exit_reason: Callable[[N], str | None],
    ) -> None:
        """Poll the running neuron until shutdown, or exit on a watchdog fault."""
        while not stop.is_set():
            self.force_restart_if_rpc_abandoned(neuron)
            if (reason := exit_reason(neuron)) is not None:
                # The worker may have died by latching between the check above
                # and this liveness probe; a plain SystemExit here would take
                # the normal exit the latch exists to prevent.
                self.force_restart_if_rpc_abandoned(neuron)
                bt.logging.error(f"{self.name} watchdog exiting: {reason}")
                self.schedule_forced_exit()
                raise SystemExit(1)
            bt.logging.info(f"{self.name.capitalize()} running... {time.time()}")
            stop.wait(_WATCHDOG_POLL_SECONDS)
        # A shutdown signal that races the latch must not fall through to the
        # normal exit the latch exists to prevent.
        self.force_restart_if_rpc_abandoned(neuron)
