"""Tests for the opt-in log drain and structured console output."""

from __future__ import annotations

import json
import logging
import queue
import socket
import socketserver
import threading

import pytest

from endure.utils.log_shipping import (
    LOG_DRAIN_ENV,
    LOG_FORMAT_ENV,
    BoundedQueueHandler,
    DrainTarget,
    JsonLineFormatter,
    ResilientSyslogHandler,
    SyslogFrameFormatter,
    configure_log_shipping,
    parse_drain_url,
)


def _record(
    message: str, *, level: int = logging.INFO, name: str = "bittensor"
) -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


class TestParseDrainUrl:
    def test_parses_each_supported_scheme(self) -> None:
        for scheme in ("syslog+udp", "syslog+tcp", "syslog+tls"):
            target = parse_drain_url(f"{scheme}://logs.example.com:6514")
            assert target == DrainTarget(
                scheme=scheme, host="logs.example.com", port=6514
            )

    def test_rejects_unknown_scheme(self) -> None:
        with pytest.raises(ValueError, match="scheme"):
            parse_drain_url("https://logs.example.com:6514")

    def test_rejects_missing_port(self) -> None:
        with pytest.raises(ValueError, match="host and port"):
            parse_drain_url("syslog+udp://logs.example.com")


class TestSyslogFrameFormatter:
    def test_formats_rfc5424_frame_with_severity(self) -> None:
        frame = SyslogFrameFormatter("endure-validator").format(
            _record("weights confirmed", level=logging.ERROR)
        )

        # Facility user(1) * 8 + severity error(3) = 11.
        assert frame.startswith("<11>1 ")
        assert " endure-validator - - - weights confirmed" in frame

    def test_sanitizes_control_and_bidi_characters(self) -> None:
        frame = SyslogFrameFormatter("endure-validator").format(
            _record("miner said\r\nfake line\u202e\u061c end")
        )

        assert "\r" not in frame and "\u202e" not in frame and "\u061c" not in frame
        assert frame.count("\n") == 0


class TestJsonLineFormatter:
    def test_emits_one_json_object_per_line(self) -> None:
        line = JsonLineFormatter().format(_record("round opened"))

        payload = json.loads(line)
        assert payload["level"] == "INFO"
        assert payload["message"] == "round opened"
        assert "\n" not in line


class TestBoundedQueueHandler:
    def test_drops_instead_of_blocking_when_full(self) -> None:
        handler = BoundedQueueHandler(queue.Queue(maxsize=1))

        handler.emit(_record("first"))
        handler.emit(_record("second"))

        assert handler.dropped_records == 1


class TestResilientSyslogHandler:
    def test_ships_frames_over_udp(self) -> None:
        receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(5)
        port = receiver.getsockname()[1]
        handler = ResilientSyslogHandler(
            DrainTarget(scheme="syslog+udp", host="127.0.0.1", port=port)
        )
        handler.setFormatter(SyslogFrameFormatter("endure-validator"))

        handler.emit(_record("shipped over udp"))
        payload = receiver.recv(65535).decode()
        handler.close()
        receiver.close()

        assert payload.endswith("shipped over udp\n")

    def test_ships_frames_over_tcp(self) -> None:
        received = threading.Event()
        lines: list[bytes] = []

        class _Collector(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                lines.append(self.rfile.readline())
                received.set()

        with socketserver.TCPServer(("127.0.0.1", 0), _Collector) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            handler = ResilientSyslogHandler(
                DrainTarget(
                    scheme="syslog+tcp",
                    host="127.0.0.1",
                    port=server.server_address[1],
                )
            )
            handler.setFormatter(SyslogFrameFormatter("endure-miner"))
            handler.emit(_record("shipped over tcp"))
            assert received.wait(timeout=5)
            handler.close()
            server.shutdown()

        assert lines and lines[0].decode().endswith("shipped over tcp\n")

    def test_unreachable_collector_drops_frames_without_raising(self) -> None:
        dead_port_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        dead_port_probe.bind(("127.0.0.1", 0))
        dead_port = dead_port_probe.getsockname()[1]
        dead_port_probe.close()
        handler = ResilientSyslogHandler(
            DrainTarget(scheme="syslog+tcp", host="127.0.0.1", port=dead_port),
            timeout_seconds=0.5,
        )
        handler.setFormatter(SyslogFrameFormatter("endure-validator"))

        handler.emit(_record("nobody listening"))
        handler.close()

        assert handler.dropped_frames == 1


class TestConfigureLogShipping:
    def test_no_environment_is_a_no_op(self) -> None:
        bittensor_logger = logging.getLogger("bittensor")
        before = list(bittensor_logger.handlers)

        assert configure_log_shipping("endure-validator", environ={}) is None
        assert bittensor_logger.handlers == before

    def test_rejects_unknown_console_format(self) -> None:
        with pytest.raises(ValueError, match="supports only 'json'"):
            configure_log_shipping(
                "endure-validator", environ={LOG_FORMAT_ENV: "logfmt"}
            )

    def test_drain_attaches_and_ships_bittensor_records(self) -> None:
        receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(5)
        port = receiver.getsockname()[1]
        bittensor_logger = logging.getLogger("bittensor")
        before = list(bittensor_logger.handlers)

        listener = configure_log_shipping(
            "endure-validator",
            environ={LOG_DRAIN_ENV: f"syslog+udp://127.0.0.1:{port}"},
        )
        try:
            assert listener is not None
            attached = [
                handler
                for handler in bittensor_logger.handlers
                if isinstance(handler, BoundedQueueHandler)
            ]
            assert len(attached) == 1
            bittensor_logger.warning("drained record")
            payload = receiver.recv(65535).decode()
            assert payload.endswith("drained record\n")
            assert payload.startswith("<12>1 ")
        finally:
            for handler in bittensor_logger.handlers:
                if isinstance(handler, BoundedQueueHandler):
                    bittensor_logger.removeHandler(handler)
            assert listener is not None
            listener.stop()
            receiver.close()
        assert [
            handler
            for handler in bittensor_logger.handlers
            if isinstance(handler, BoundedQueueHandler)
        ] == []
        assert bittensor_logger.handlers == before
