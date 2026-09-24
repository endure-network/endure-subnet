"""Process-level shutdown signalling shared by the neuron entrypoints."""

from __future__ import annotations

import atexit
import os
import signal
import sys
import threading
import traceback
from collections.abc import Callable
from types import FrameType
from typing import NoReturn

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
    atexit._run_exitfuncs()  # noqa: SLF001 — os._exit skips registered drains
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
