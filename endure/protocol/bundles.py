"""Miner-side bundle assembly (spec §4, §6).

Builds the canonical bundle bytes a miner sends: outputs are validated through
the schema model, serialized canonically (order-independent), and hashed with a
random nonce — the pair the commit/reveal windows carry.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from endure.protocol.canonical import (
    COMMIT_NONCE_BYTES,
    canonical_bundle_bytes,
    commit_hash,
)
from endure.protocol.validation import CanonicalBundle


@dataclass(frozen=True, slots=True)
class AssembledSubmission:
    bundle_json: str
    nonce_hex: str
    bundle_hash: str


def assemble_bundle(
    bundle: CanonicalBundle,
    *,
    miner_hotkey: str,
    nonce: bytes | None = None,
) -> AssembledSubmission:
    """Serialize and hash any canonical bundle into a commit/reveal pair."""
    bundle_bytes = canonical_bundle_bytes(bundle.to_canonical_payload())
    nonce_bytes = (
        nonce if nonce is not None else secrets.token_bytes(COMMIT_NONCE_BYTES)
    )
    if len(nonce_bytes) != COMMIT_NONCE_BYTES:
        raise ValueError(f"nonce must be {COMMIT_NONCE_BYTES} bytes")
    return AssembledSubmission(
        bundle_json=bundle_bytes.decode("utf-8"),
        nonce_hex=nonce_bytes.hex(),
        bundle_hash=commit_hash(bundle_bytes, nonce_bytes, miner_hotkey=miner_hotkey),
    )
