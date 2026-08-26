"""Public Alpha Risk feed verifier tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from bittensor_wallet import Keypair

from endure.protocol.canonical import canonical_bundle_bytes
from verify.risk_feed import main, verify_risk_feed, verify_risk_feed_file


def _signed_feed() -> tuple[dict[str, object], Keypair]:
    keypair = Keypair.create_from_uri("//Alice")
    payload: dict[str, object] = {
        "schema_id": "risk.v1.subnet_alpha",
        "feed_schema_version": 1,
        "round_id": "2026-08-26",
        "as_of": "2026-08-26T20:00:00+00:00",
        "subnets": [],
    }
    canonical = canonical_bundle_bytes(payload)
    return (
        {
            "payload": payload,
            "signature": {
                "signed_by": keypair.ss58_address,
                "signature_hex": keypair.sign(canonical).hex(),
                "canonical_payload_sha256": hashlib.sha256(canonical).hexdigest(),
            },
        },
        keypair,
    )


def test_verifies_canonical_hash_signature_and_identity(tmp_path: Path) -> None:
    document, keypair = _signed_feed()
    path = tmp_path / "risk-feed.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    result = verify_risk_feed_file(path)

    assert result.signer == keypair.ss58_address
    assert result.round_id == "2026-08-26"
    payload = document["payload"]
    assert isinstance(payload, dict)
    assert (
        result.canonical_payload_sha256
        == hashlib.sha256(canonical_bundle_bytes(payload)).hexdigest()
    )


def test_rejects_tampered_payload_before_signature_check() -> None:
    document, _ = _signed_feed()
    payload = document["payload"]
    assert isinstance(payload, dict)
    payload["round_id"] = "tampered"

    with pytest.raises(ValueError, match="SHA-256 does not match"):
        verify_risk_feed(document)


def test_rejects_invalid_signature_with_matching_hash() -> None:
    document, _ = _signed_feed()
    payload = document["payload"]
    signature = document["signature"]
    assert isinstance(payload, dict)
    assert isinstance(signature, dict)
    signature["signature_hex"] = "00" * 64
    signature["canonical_payload_sha256"] = hashlib.sha256(
        canonical_bundle_bytes(payload)
    ).hexdigest()

    with pytest.raises(ValueError, match="invalid signature"):
        verify_risk_feed(document)


def test_cli_reports_identity_without_quorum_claim(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document, keypair = _signed_feed()
    path = tmp_path / "risk-feed.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    assert main(["--file", str(path)]) == 0
    output = capsys.readouterr().out
    assert f"signer: {keypair.ss58_address}" in output
    assert "round_id: 2026-08-26" in output
    assert "no quorum or correctness claim" in output
