"""Process-level shutdown signalling shared by the neuron entrypoints."""

from __future__ import annotations

import signal
import threading
from types import FrameType

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
