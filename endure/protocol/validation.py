"""Commit/reveal verdicts (spec §6) — pure and clock-free.

The axon handlers bind these to stored state; everything here takes ``now``
and window boundaries as arguments so verdicts are replayable. Reveal
validation enforces canonical bytes: a bundle that parses but is not
canonically encoded is rejected, because non-canonical encodings would break
hash verification and replay determinism.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from endure.assessment.registry import (
    SchemaRegistry,
    SchemaRegistryEntry,
    UnknownSchemaError,
    default_registry,
)
from endure.assessment.schemas.wire import OUT_OF_BOUNDS_ERROR
from endure.protocol.canonical import (
    COMMIT_HASH_DIGEST_SIZE,
    COMMIT_NONCE_BYTES,
    canonical_bundle_bytes,
    commit_hash,
)
from endure.protocol.synapses import RejectionCode
from endure.protocol.version_contract import CURRENT_VERSION_KEY

_DEFAULT_REGISTRY = default_registry()

# Wire caps, checked before any hashing or parsing so adversarial payloads
# cost one length check. 256 KiB ≈ 5x the largest legitimate bundle
# (MAX_PREDICTIONS_PER_BUNDLE rows at ~200 canonical bytes each); miners
# send 16-byte nonces, 32 is insurance. round_id is an ISO date (10 chars);
# 64 is headroom and bounds the string before it reaches a DB query.
MAX_REVEAL_BUNDLE_BYTES = 262_144
MAX_NONCE_BYTES = 32
MAX_ROUND_ID_LENGTH = 64


@runtime_checkable
class CanonicalBundle(Protocol):
    """Parsed reveal bundle surface shared by registered schemas.

    ``round_id`` and ``schema_id`` are read-only properties so frozen bundle
    models whose ``schema_id`` narrows to a ``Literal`` still satisfy the
    protocol (mutable attributes would make the field invariant).
    """

    @property
    def round_id(self) -> str: ...

    @property
    def schema_id(self) -> str: ...

    def to_canonical_payload(self) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class Verdict:
    accepted: bool
    rejection_code: RejectionCode | None = None
    bundle: CanonicalBundle | None = None


def _reject(code: RejectionCode) -> Verdict:
    return Verdict(accepted=False, rejection_code=code)


def reveal_payload_rejection(bundle_json: str, nonce_hex: str) -> RejectionCode | None:
    try:
        bundle_bytes_len = len(bundle_json.encode("utf-8"))
    except UnicodeEncodeError:
        return RejectionCode.MALFORMED_BUNDLE
    if (
        bundle_bytes_len > MAX_REVEAL_BUNDLE_BYTES
        or len(nonce_hex) > MAX_NONCE_BYTES * 2
    ):
        return RejectionCode.PAYLOAD_TOO_LARGE
    return None


def _registry_entry(
    schema_id: str, registry: SchemaRegistry | None
) -> SchemaRegistryEntry | None:
    try:
        return (registry or _DEFAULT_REGISTRY).get(schema_id)
    except UnknownSchemaError:
        return None


def _is_hex_digest(value: str) -> bool:
    if len(value) != COMMIT_HASH_DIGEST_SIZE * 2:
        return False
    if value.lower() != value:
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True


def is_canonical_round_id(value: str) -> bool:
    """A round id is exactly the canonical ISO session date."""
    if len(value) > MAX_ROUND_ID_LENGTH:
        return False
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return False
    return parsed.isoformat() == value


def validate_commit(
    *,
    round_id: str,
    schema_id: str,
    spec_version: int,
    bundle_hash: str,
    now: datetime,
    commit_open: datetime,
    commit_close: datetime,
    registry: SchemaRegistry | None = None,
) -> Verdict:
    """Verdict for a SubmitCommit. ``round_id`` is the caller-matched round."""
    if not is_canonical_round_id(round_id):
        return _reject(RejectionCode.MALFORMED_BUNDLE)
    if spec_version != CURRENT_VERSION_KEY:
        return _reject(RejectionCode.VERSION_MISMATCH)
    if _registry_entry(schema_id, registry) is None:
        return _reject(RejectionCode.UNKNOWN_SCHEMA)
    if not commit_open <= now <= commit_close:
        return _reject(RejectionCode.LATE_COMMIT)
    if not _is_hex_digest(bundle_hash):
        return _reject(RejectionCode.MALFORMED_BUNDLE)
    return Verdict(accepted=True)


def _classify_validation_error(error: ValidationError) -> RejectionCode:
    for item in error.errors():
        if item["type"] == OUT_OF_BOUNDS_ERROR:
            return RejectionCode.OUT_OF_BOUNDS
    return RejectionCode.MALFORMED_BUNDLE


def _validate_reveal_bundle(
    *,
    entry: SchemaRegistryEntry,
    round_id: str,
    schema_id: str,
    bundle_json: str,
    universe: tuple[str, ...],
) -> Verdict:
    # A size-legal but adversarial payload must yield a verdict, not an
    # exception: >4300-digit integer literals raise a plain ValueError (not
    # JSONDecodeError) and deeply nested arrays raise RecursionError.
    try:
        payload = json.loads(bundle_json)
    except (ValueError, RecursionError):
        return _reject(RejectionCode.MALFORMED_BUNDLE)
    try:
        parsed = entry.bundle_model.model_validate(payload)
    except ValidationError as error:
        return _reject(_classify_validation_error(error))
    if not isinstance(parsed, CanonicalBundle):
        return _reject(RejectionCode.MALFORMED_BUNDLE)
    bundle = parsed
    if bundle.round_id != round_id or bundle.schema_id != schema_id:
        return _reject(RejectionCode.MALFORMED_BUNDLE)

    if canonical_bundle_bytes(bundle.to_canonical_payload()) != bundle_json.encode(
        "utf-8"
    ):
        return _reject(RejectionCode.MALFORMED_BUNDLE)

    membership_valid = entry.bundle_membership_valid
    if membership_valid is not None and not membership_valid(bundle, universe):
        return _reject(RejectionCode.INVALID_TICKER)

    return Verdict(accepted=True, bundle=bundle)


def validate_reveal(
    *,
    round_id: str,
    schema_id: str,
    spec_version: int,
    bundle_json: str,
    nonce_hex: str,
    committed_hash: str | None,
    miner_hotkey: str,
    now: datetime,
    reveal_open: datetime,
    reveal_close: datetime,
    universe: tuple[str, ...],
    registry: SchemaRegistry | None = None,
) -> Verdict:
    """Verdict for a SubmitReveal; parses the bundle on acceptance.

    ``miner_hotkey`` is the signature-verified envelope hotkey — the commit
    digest is recomputed against it, so a preimage committed by one hotkey
    can never be revealed by another.
    """
    if not is_canonical_round_id(round_id):
        return _reject(RejectionCode.MALFORMED_BUNDLE)
    if spec_version != CURRENT_VERSION_KEY:
        return _reject(RejectionCode.VERSION_MISMATCH)
    entry = _registry_entry(schema_id, registry)
    if entry is None:
        return _reject(RejectionCode.UNKNOWN_SCHEMA)
    payload_rejection = reveal_payload_rejection(bundle_json, nonce_hex)
    if payload_rejection is not None:
        return _reject(payload_rejection)
    if not reveal_open <= now <= reveal_close:
        return _reject(RejectionCode.LATE_REVEAL)
    if committed_hash is None:
        return _reject(RejectionCode.NO_COMMIT)

    try:
        nonce = bytes.fromhex(nonce_hex)
    except ValueError:
        return _reject(RejectionCode.MALFORMED_BUNDLE)
    if len(nonce) < COMMIT_NONCE_BYTES:
        return _reject(RejectionCode.MALFORMED_BUNDLE)
    recomputed = commit_hash(
        bundle_json.encode("utf-8"), nonce, miner_hotkey=miner_hotkey
    )
    if recomputed != committed_hash:
        return _reject(RejectionCode.HASH_MISMATCH)

    return _validate_reveal_bundle(
        entry=entry,
        round_id=round_id,
        schema_id=schema_id,
        bundle_json=bundle_json,
        universe=universe,
    )
