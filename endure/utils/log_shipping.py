"""Opt-in log shipping and structured console output for neuron operators.

Disabled unless the operator sets the environment variables, so a default
deployment behaves exactly as before:

- ``ENDURE_LOG_DRAIN=syslog+udp://host:port`` (or ``syslog+tcp`` /
  ``syslog+tls``) ships every bittensor log record to a remote syslog
  collector (Papertrail, Better Stack, rsyslog, promtail, ...).
- ``ENDURE_LOG_FORMAT=json`` switches console output to one JSON object per
  line for container-level collectors.

Shipping must never be able to wedge a neuron: records cross a bounded
in-process queue (drop-on-overflow, never block), the network emitter runs on
its own daemon thread with lazy reconnect, and shipped text passes
``safe_remote_text`` so miner-controlled strings leave the box sanitized.
"""

from __future__ import annotations

import atexit
import json
import logging
import logging.handlers
import os
import queue
import socket
import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from urllib.parse import urlsplit

import bittensor as bt

from endure.utils.logging import safe_error, safe_remote_text

LOG_DRAIN_ENV: Final = "ENDURE_LOG_DRAIN"
LOG_FORMAT_ENV: Final = "ENDURE_LOG_FORMAT"
DRAIN_SCHEMES: Final = frozenset({"syslog+udp", "syslog+tcp", "syslog+tls"})
DRAIN_QUEUE_CAPACITY: Final = 1000
DRAIN_CONNECT_TIMEOUT_SECONDS: Final = 5.0
DRAIN_RECONNECT_COOLDOWN_SECONDS: Final = 30.0
DRAIN_MAX_MESSAGE_CHARS: Final = 8192
_SYSLOG_FACILITY_USER: Final = 1
_SYSLOG_SEVERITY_BY_LEVEL: Final = (
    (logging.CRITICAL, 2),
    (logging.ERROR, 3),
    (logging.WARNING, 4),
    (logging.INFO, 6),
)
_SYSLOG_SEVERITY_DEBUG: Final = 7


@dataclass(frozen=True, slots=True)
class DrainTarget:
    scheme: str
    host: str
    port: int


def parse_drain_url(url: str) -> DrainTarget:
    parts = urlsplit(url)
    if parts.scheme not in DRAIN_SCHEMES:
        raise ValueError(
            f"{LOG_DRAIN_ENV} scheme must be one of {sorted(DRAIN_SCHEMES)}: {url!r}"
        )
    if not parts.hostname or parts.port is None:
        raise ValueError(f"{LOG_DRAIN_ENV} must include host and port: {url!r}")
    return DrainTarget(scheme=parts.scheme, host=parts.hostname, port=parts.port)


def _syslog_severity(levelno: int) -> int:
    for threshold, severity in _SYSLOG_SEVERITY_BY_LEVEL:
        if levelno >= threshold:
            return severity
    return _SYSLOG_SEVERITY_DEBUG


class SyslogFrameFormatter(logging.Formatter):
    """RFC 5424 frames with the record text passed through safe_remote_text."""

    def __init__(self, app_name: str) -> None:
        super().__init__()
        self._app_name = app_name
        self._hostname = socket.gethostname() or "unknown"

    def format(self, record: logging.LogRecord) -> str:
        # No exc_info branch on purpose: QueueHandler.prepare() runs on the
        # emitting thread, folds the formatted traceback into the message,
        # and nulls exc_info before enqueueing — so the traceback arrives
        # here inside getMessage() and passes through the same sanitizer.
        message = safe_remote_text(
            record.getMessage(), max_length=DRAIN_MAX_MESSAGE_CHARS
        )
        priority = _SYSLOG_FACILITY_USER * 8 + _syslog_severity(record.levelno)
        timestamp = datetime.fromtimestamp(record.created, UTC).isoformat()
        return (
            f"<{priority}>1 {timestamp} {self._hostname} {self._app_name} "
            f"- - - {message}"
        )


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, str] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "name": record.name,
            "message": safe_error(record.getMessage()),
        }
        if record.exc_info:
            payload["exception"] = safe_error(self.formatException(record.exc_info))
        return json.dumps(payload, sort_keys=True)


class BoundedQueueHandler(logging.handlers.QueueHandler):
    """Drops records when the drain queue is full instead of blocking the
    emitting thread or spamming stderr through handleError."""

    def __init__(self, record_queue: queue.Queue[logging.LogRecord]) -> None:
        super().__init__(record_queue)
        self.dropped_records = 0

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped_records += 1


class ResilientSyslogHandler(logging.Handler):
    """Syslog emitter with lazy connect and reconnect-on-next-emit.

    Runs only on the drain listener thread. A dead collector costs dropped
    frames, never a blocked neuron: connect attempts are timeout-bounded and
    any OSError closes the socket and drops the frame.
    """

    def __init__(
        self,
        target: DrainTarget,
        *,
        timeout_seconds: float = DRAIN_CONNECT_TIMEOUT_SECONDS,
        reconnect_cooldown_seconds: float = DRAIN_RECONNECT_COOLDOWN_SECONDS,
        ssl_context: ssl.SSLContext | None = None,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self._target = target
        self._timeout_seconds = timeout_seconds
        self._reconnect_cooldown_seconds = reconnect_cooldown_seconds
        self._ssl_context = ssl_context
        self._now_fn = now_fn
        self._retry_at_monotonic = 0.0
        self._socket: socket.socket | None = None
        self.dropped_frames = 0

    def _connect(self) -> socket.socket:
        if self._target.scheme == "syslog+udp":
            return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        raw = socket.create_connection(
            (self._target.host, self._target.port), timeout=self._timeout_seconds
        )
        if self._target.scheme != "syslog+tls":
            return raw
        context = self._ssl_context or ssl.create_default_context()
        try:
            return context.wrap_socket(raw, server_hostname=self._target.host)
        except OSError:
            raw.close()
            raise

    def emit(self, record: logging.LogRecord) -> None:
        frame = (self.format(record) + "\n").encode("utf-8", errors="replace")
        if self._socket is None and self._now_fn() < self._retry_at_monotonic:
            # Cooldown after a failed collector: without it, every queued
            # record pays a full connect timeout against a dead endpoint and
            # drain throughput collapses to one frame per timeout.
            self.dropped_frames += 1
            return
        try:
            if self._socket is None:
                self._socket = self._connect()
            if self._target.scheme == "syslog+udp":
                self._socket.sendto(frame, (self._target.host, self._target.port))
            else:
                self._socket.sendall(frame)
        except OSError:
            self.dropped_frames += 1
            self._close_socket()
            self._retry_at_monotonic = self._now_fn() + self._reconnect_cooldown_seconds

    def _close_socket(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None

    def close(self) -> None:
        self._close_socket()
        super().close()


class DrainQueueListener(logging.handlers.QueueListener):
    """QueueListener whose stop() is idempotent: both atexit and an explicit
    caller (tests, future teardown paths) may stop it without a second stop
    crashing on the already-joined thread."""

    def stop(self) -> None:
        if self._thread is not None:
            super().stop()


def _apply_console_format(environ: Mapping[str, str]) -> None:
    log_format = environ.get(LOG_FORMAT_ENV)
    if log_format is None:
        return
    if log_format.lower() != "json":
        raise ValueError(f"{LOG_FORMAT_ENV} supports only 'json': {log_format!r}")
    handlers = getattr(bt.logging, "_handlers", None)
    if not handlers:
        bt.logging.warning(
            "ENDURE_LOG_FORMAT=json ignored: bittensor logging exposes no handlers"
        )
        return
    formatter = JsonLineFormatter()
    for handler in handlers:
        if isinstance(handler, logging.StreamHandler) and not isinstance(
            handler, logging.FileHandler
        ):
            handler.setFormatter(formatter)


def configure_log_shipping(
    app_name: str,
    environ: Mapping[str, str] = os.environ,
) -> logging.handlers.QueueListener | None:
    """Apply the opt-in logging environment; returns the started drain
    listener (None when no drain is configured). Malformed configuration
    raises so a typo fails the boot loudly instead of silently not shipping.
    """
    _apply_console_format(environ)
    url = environ.get(LOG_DRAIN_ENV)
    if not url:
        return None
    target = parse_drain_url(url)
    drain = ResilientSyslogHandler(target)
    drain.setFormatter(SyslogFrameFormatter(app_name))
    record_queue: queue.Queue[logging.LogRecord] = queue.Queue(
        maxsize=DRAIN_QUEUE_CAPACITY
    )
    listener = DrainQueueListener(record_queue, drain, respect_handler_level=True)
    listener.start()
    atexit.register(listener.stop)
    logging.getLogger("bittensor").addHandler(BoundedQueueHandler(record_queue))
    bt.logging.info(f"log drain enabled: {target.scheme}://{target.host}:{target.port}")
    return listener
