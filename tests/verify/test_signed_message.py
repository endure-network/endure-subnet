from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from bittensor_wallet import Keypair

from verify.generate import (
    DEFAULT_OUTPUT,
    build_signed_payload,
    build_wrapped_message,
    write_signed_payload,
)
from verify.verify import load_signed_payload, verify_payload


def test_signed_payload_round_trips() -> None:
    keypair = Keypair.create_from_uri("//Alice")
    payload = build_signed_payload(
        keypair,
        "hello\n\tworld",
        timestamp=datetime(2026, 4, 21, 12, 0, 0),
    )

    verified_address = verify_payload(payload)

    assert verified_address == keypair.ss58_address
    assert payload["message"].startswith("<Bytes>")
    assert payload["message"].endswith("</Bytes>")


def test_write_and_load_signed_payload_round_trip(tmp_path: Path) -> None:
    keypair = Keypair.create_from_uri("//Alice")
    payload = build_signed_payload(keypair, "payload", timestamp=datetime(2026, 4, 21))
    output_path = tmp_path / "nested" / "message_and_signature.json"

    write_signed_payload(payload, output_path)
    loaded = load_signed_payload(output_path)

    assert loaded == payload


def test_write_signed_payload_sets_owner_only_permissions(tmp_path: Path) -> None:
    keypair = Keypair.create_from_uri("//Alice")
    payload = build_signed_payload(keypair, "secret", timestamp=datetime(2026, 4, 21))
    output_path = tmp_path / "message_and_signature.json"

    write_signed_payload(payload, output_path)

    assert output_path.stat().st_mode & 0o777 == 0o600


def test_load_signed_payload_rejects_missing_keys(tmp_path: Path) -> None:
    output_path = tmp_path / "bad.json"
    output_path.write_text(
        json.dumps({"message": "<Bytes>x</Bytes>"}), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="missing required keys"):
        load_signed_payload(output_path)


def test_load_signed_payload_rejects_non_string_field(tmp_path: Path) -> None:
    output_path = tmp_path / "bad.json"
    output_path.write_text(
        json.dumps(
            {
                "message": "<Bytes>x</Bytes>",
                "signed_by": "addr",
                "signature_hex": 123,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must be a string"):
        load_signed_payload(output_path)


def test_verify_payload_rejects_tampered_signature() -> None:
    keypair = Keypair.create_from_uri("//Alice")
    payload = build_signed_payload(
        keypair, "hello", timestamp=datetime(2026, 4, 21, 12, 0, 0)
    )
    tampered = dict(payload)
    tampered["signature_hex"] = "00" * 64

    with pytest.raises(ValueError, match="Invalid signature"):
        verify_payload(tampered)


def test_verify_payload_rejects_unwrapped_message() -> None:
    keypair = Keypair.create_from_uri("//Alice")
    payload = {
        "message": "On 2026 hello",
        "signed_by": keypair.ss58_address,
        "signature_hex": "00",
    }

    with pytest.raises(ValueError, match="not properly wrapped"):
        verify_payload(payload)


def test_default_output_is_gitignored_secret_path() -> None:
    assert DEFAULT_OUTPUT == Path(".secrets/message_and_signature.json")


def test_build_wrapped_message_preserves_embedded_newlines() -> None:
    message = build_wrapped_message(
        "line1\n\tline2",
        timestamp=datetime(2026, 4, 21, 12, 0, 0),
    )

    assert "line1\n\tline2" in message
    assert message.startswith("<Bytes>")
    assert message.endswith("</Bytes>")
