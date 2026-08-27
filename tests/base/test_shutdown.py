"""Tests for endure.base.shutdown."""

from __future__ import annotations

import signal
import threading
from collections.abc import Iterator

import pytest

from endure.base.shutdown import SHUTDOWN_SIGNALS, install_shutdown_handlers


@pytest.fixture
def restore_signal_handlers() -> Iterator[None]:
    saved = {signum: signal.getsignal(signum) for signum in SHUTDOWN_SIGNALS}
    yield
    for signum, handler in saved.items():
        signal.signal(signum, handler)


def test_sigint_and_sigterm_share_one_graceful_stop(
    restore_signal_handlers: None,
) -> None:
    stop = install_shutdown_handlers()

    sigint_handler = signal.getsignal(signal.SIGINT)
    sigterm_handler = signal.getsignal(signal.SIGTERM)
    assert callable(sigint_handler)
    assert sigint_handler is sigterm_handler
    assert not stop.is_set()

    sigint_handler(signal.SIGINT, None)

    assert stop.is_set()


def test_sigterm_sets_the_same_stop_event(restore_signal_handlers: None) -> None:
    stop = install_shutdown_handlers()
    handler = signal.getsignal(signal.SIGTERM)
    assert callable(handler)

    handler(signal.SIGTERM, None)

    assert stop.is_set()


def test_replaces_an_inherited_ignored_sigint(restore_signal_handlers: None) -> None:
    # A background job started by a non-interactive shell inherits SIG_IGN.
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    install_shutdown_handlers()

    assert signal.getsignal(signal.SIGINT) is not signal.SIG_IGN


def test_stop_event_wakes_a_waiting_main_loop(restore_signal_handlers: None) -> None:
    stop = install_shutdown_handlers()
    handler = signal.getsignal(signal.SIGINT)
    assert callable(handler)
    timer = threading.Timer(0.05, handler, args=(signal.SIGINT, None))
    timer.start()
    try:
        assert stop.wait(5) is True
    finally:
        timer.cancel()
