"""Verify the integrity and signer of one Endure Alpha Risk feed file."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from bittensor_wallet import Keypair

from endure.protocol.canonical import canonical_bundle_bytes


@dataclass(frozen=True, slots=True)
class VerifiedRiskFeed:
    signer: str
    round_id: str | None
    canonical_payload_sha256: str


def _object_mapping(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be a JSON object with string keys")
    return {str(key): item for key, item in value.items()}


def _required_string(mapping: dict[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"signature.{key} must be a non-empty string")
    return value


def verify_risk_feed(document: object) -> VerifiedRiskFeed:
    """Verify canonical hash and signature; do not infer quorum or correctness."""
    envelope = _object_mapping(document, label="feed")
    payload = _object_mapping(envelope.get("payload"), label="payload")
    signature = _object_mapping(envelope.get("signature"), label="signature")
    signed_by = _required_string(signature, "signed_by")
    signature_hex = _required_string(signature, "signature_hex")
    expected_hash = _required_string(signature, "canonical_payload_sha256")

    canonical_payload = canonical_bundle_bytes(payload)
    actual_hash = hashlib.sha256(canonical_payload).hexdigest()
    if not hmac.compare_digest(expected_hash, actual_hash):
        raise ValueError("canonical payload SHA-256 does not match")
    try:
        signature_bytes = bytes.fromhex(signature_hex)
    except ValueError as error:
        raise ValueError("signature.signature_hex must be hexadecimal") from error
    keypair = Keypair(ss58_address=signed_by, ss58_format=42)
    if not keypair.verify(data=canonical_payload, signature=signature_bytes):
        raise ValueError(f"invalid signature for signer {signed_by}")

    round_id = payload.get("round_id")
    if round_id is not None and not isinstance(round_id, str):
        raise ValueError("payload.round_id must be a string or null")
    return VerifiedRiskFeed(
        signer=signed_by,
        round_id=round_id,
        canonical_payload_sha256=actual_hash,
    )


def verify_risk_feed_file(path: Path) -> VerifiedRiskFeed:
    try:
        document: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"risk feed cannot be read: {error}") from error
    return verify_risk_feed(document)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify one Endure risk-feed hash and signer. This does not prove "
            "validator independence, quorum, or forecast correctness."
        )
    )
    parser.add_argument("--file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = verify_risk_feed_file(args.file)
    except ValueError as error:
        print(f"Risk feed verification failed: {error}", file=sys.stderr)
        return 1
    print(f"signer: {result.signer}")
    print(f"round_id: {result.round_id}")
    print(f"canonical_payload_sha256: {result.canonical_payload_sha256}")
    print("verified: integrity and signer only; no quorum or correctness claim")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
