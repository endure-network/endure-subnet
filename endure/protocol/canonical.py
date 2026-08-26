"""Canonical wire serialization + commit hashing (spec §4, §6).

Wire bundles carry bps values as integers. Floats and Decimals are rejected at
serialization time so numeric semantics never depend on JSON number parsing,
and identical payloads always produce identical bytes (sorted keys, compact
separators, UTF-8). Commit hashes are blake2b-256 over
``bundle || nonce || miner-hotkey`` — hotkey-bound so preimages are not
replayable across identities.
"""

from __future__ import annotations

import hashlib
import json

COMMIT_HASH_DIGEST_SIZE = 32
COMMIT_NONCE_BYTES = 16


class CanonicalizationError(ValueError):
    """Raised when a payload cannot be canonically serialized."""


def _validate(node: object) -> None:
    if isinstance(node, float):
        raise CanonicalizationError(
            "float values are not canonicalizable; encode bps as integers"
        )
    if node is None or isinstance(node, (bool, int, str)):
        return
    if isinstance(node, list):
        for item in node:
            _validate(item)
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if not isinstance(key, str):
                raise CanonicalizationError(
                    f"object keys must be str, got {type(key).__name__}"
                )
            _validate(value)
        return
    raise CanonicalizationError(
        f"unsupported type for canonical serialization: {type(node).__name__}"
    )


def canonical_bundle_bytes(payload: object) -> bytes:
    """Serialize ``payload`` to canonical UTF-8 JSON bytes."""
    _validate(payload)
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def commit_hash(bundle_bytes: bytes, nonce: bytes, *, miner_hotkey: str) -> str:
    """Hex blake2b-256 digest over ``bundle_bytes || nonce || hotkey-utf8``.

    Binding the submitting hotkey into the preimage means a leaked
    bundle+nonce cannot be replayed by another registered hotkey — the
    validator recomputes with the signature-verified envelope hotkey.
    """
    return hashlib.blake2b(
        bundle_bytes + nonce + miner_hotkey.encode("utf-8"),
        digest_size=COMMIT_HASH_DIGEST_SIZE,
    ).hexdigest()
