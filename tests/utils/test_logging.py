"""Tests for endure.utils.logging.setup_events_logger."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from endure.utils.logging import (
    DEFAULT_LOG_BACKUP_COUNT,
    EVENTS_LEVEL_NUM,
    safe_endpoint_label,
    safe_error,
    safe_remote_text,
    setup_events_logger,
    startup_config_summary,
)


class TestSetupEventsLogger:
    def test_returns_configured_logger_and_writes_record(self, tmp_path: Path) -> None:
        max_bytes = 1024
        logger = setup_events_logger(str(tmp_path), max_bytes)

        assert logger.name == "event"
        assert logger.level == EVENTS_LEVEL_NUM

        rotating_handlers = [
            h for h in logger.handlers if isinstance(h, RotatingFileHandler)
        ]
        assert rotating_handlers, "expected a RotatingFileHandler attached"

        handler = rotating_handlers[-1]
        assert handler.maxBytes == max_bytes
        assert handler.backupCount == DEFAULT_LOG_BACKUP_COUNT
        assert handler.level == EVENTS_LEVEL_NUM
        assert Path(handler.baseFilename) == tmp_path / "events.log"

        logger.log(EVENTS_LEVEL_NUM, "hello-event")
        handler.flush()

        events_log = tmp_path / "events.log"
        assert events_log.exists()
        contents = events_log.read_text(encoding="utf-8")
        assert "hello-event" in contents
        assert "EVENT" in contents

    def test_events_level_registered_with_standard_logging(
        self, tmp_path: Path
    ) -> None:
        setup_events_logger(str(tmp_path), 2048)
        assert logging.getLevelName(EVENTS_LEVEL_NUM) == "EVENT"

    def test_repeated_setup_does_not_duplicate_handler_for_same_path(
        self, tmp_path: Path
    ) -> None:
        setup_events_logger(str(tmp_path), 1024)
        logger = setup_events_logger(str(tmp_path), 2048)

        target = tmp_path / "events.log"
        matching = [
            h
            for h in logger.handlers
            if isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == target
        ]
        assert len(matching) == 1
        assert matching[0].maxBytes == 2048

    def test_setup_preserves_unrelated_handler(self, tmp_path: Path) -> None:
        logger = logging.getLogger("event")
        unrelated = logging.StreamHandler()
        logger.addHandler(unrelated)
        try:
            setup_events_logger(str(tmp_path), 1024)
            assert unrelated in logger.handlers
        finally:
            logger.removeHandler(unrelated)


class TestSafeLogging:
    @pytest.mark.parametrize(
        ("endpoint", "expected"),
        (
            (None, "<unset>"),
            ("test", "test"),
            ("[::1]:9944", "[::1]:9944"),
            ("https:///missing-host", "<configured>"),
            ("http://[invalid", "<configured>"),
            ("host:invalid-port", "<configured>"),
        ),
    )
    def test_endpoint_label_handles_non_url_and_malformed_values(
        self, endpoint: object, expected: str
    ) -> None:
        assert safe_endpoint_label(endpoint) == expected

    def test_remote_text_is_bounded_to_one_printable_line(self) -> None:
        secret = "super-secret-token"
        injected = (
            "ok\n2026-01-01 00:00:00 ERROR forged admin line\x1b[31m"
            + f"wss://operator:{secret}@rpc.example.org/ws "
            + "A" * 500
        )

        text = safe_remote_text(injected)

        assert "\n" not in text
        assert "\x1b" not in text
        assert "secret" not in text
        assert len(text) <= 201
        assert text.endswith("…")

    def test_remote_text_strips_unicode_separators_and_bidi_controls(self) -> None:
        injected = (
            "ok\u2028forged line\u2029tail"
            "\u202eDESREVER\u202c\u200e\u200f\u2066isolated\u2069"
        )

        text = safe_remote_text(injected)

        for forbidden in ("\u2028", "\u2029", "\u200e", "\u200f"):
            assert forbidden not in text
        for forbidden in ("\u202a", "\u202c", "\u202e", "\u2066", "\u2069"):
            assert forbidden not in text
        assert "forged line" in text
        assert "isolated" in text

    def test_endpoint_label_removes_url_secrets_and_details(self) -> None:
        secret = "super-secret-token"
        label = safe_endpoint_label(
            f"wss://operator:{secret}@rpc.example.org:9944/private?key={secret}#frag"
        )

        assert label == "wss://rpc.example.org:9944"
        assert secret not in label

    def test_startup_summary_is_allowlisted_and_sanitized(self) -> None:
        secret = "super-secret-token"

        class Section:
            pass

        config = Section()
        config.netuid = 42
        config.database_url = f"postgresql://user:{secret}@db/private"
        config.wallet = Section()
        config.wallet.name = "operator"
        config.wallet.hotkey = "validator"
        config.subtensor = Section()
        config.subtensor.chain_endpoint = (
            f"wss://user:{secret}@rpc.example.org:9944/private?key={secret}"
        )
        config.endure = Section()
        config.endure.active_schema = "risk.v1.subnet_alpha"
        config.endure.api_host = f"operator:{secret}@api.example.org"
        config.endure.api_port = 8714
        config.endure.market_data_endpoint = (
            f"https://user:{secret}@market.example.org/feed?token={secret}"
        )

        summary = startup_config_summary(config, neuron_type="ValidatorNeuron")
        rendered = repr(summary)

        assert summary["subtensor.endpoint"] == "wss://rpc.example.org:9944"
        assert summary["endure.market_data_endpoint"] == "https://market.example.org"
        assert summary["endure.api_host"] == "api.example.org"
        assert "database_url" not in summary
        assert secret not in rendered

    def test_safe_error_redacts_credential_bearing_urls(self) -> None:
        secret = "super-secret-token"
        exc = ConnectionError(
            f"failed to reach wss://user:{secret}@rpc.example.org:9944/x?token={secret}"
        )

        rendered = safe_error(exc)

        assert secret not in rendered
        assert "rpc.example.org" not in rendered
        assert "<redacted-endpoint>" in rendered
        assert rendered.startswith("failed to reach ")

    def test_safe_error_preserves_message_without_urls(self) -> None:
        assert safe_error(ValueError("bundle rejected: horizon out of range")) == (
            "bundle rejected: horizon out of range"
        )

    def test_safe_error_redacts_schemeless_credentials(self) -> None:
        secret = "super-secret-token"
        rendered = safe_error(
            OSError(f"auth failed for operator:{secret}@db.internal:5432")
        )
        assert secret not in rendered
        assert "db.internal" not in rendered
        assert "<redacted-endpoint>" in rendered

    def test_startup_summary_handles_minimal_config_and_network_fallback(
        self,
    ) -> None:
        class Section:
            pass

        config = Section()
        config.mock = False
        config.subtensor = Section()
        config.subtensor.network = "test"
        config.runtime = Section()
        config.runtime.mode = None

        summary = startup_config_summary(config, neuron_type="MinerNeuron")

        assert summary == {
            "neuron_type": "MinerNeuron",
            "mock": False,
            "subtensor.endpoint": "test",
        }


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
