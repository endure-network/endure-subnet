"""Wire synapses for the daily round (spec §6).

Two messages, miner → validator: ``SubmitCommit`` carries the blake2b-256
hash of the canonical bundle bytes plus nonce before the deadline;
``SubmitReveal`` carries the canonical bundle JSON and the nonce after the
close. Validators reply in-place via ``accepted``/``rejection_code`` — codes
are stable wire strings.
"""

from __future__ import annotations

from enum import StrEnum

import bittensor as bt


class RejectionCode(StrEnum):
    VERSION_MISMATCH = "VERSION_MISMATCH"
    UNKNOWN_SCHEMA = "UNKNOWN_SCHEMA"
    LATE_COMMIT = "LATE_COMMIT"
    NO_COMMIT = "NO_COMMIT"
    LATE_REVEAL = "LATE_REVEAL"
    HASH_MISMATCH = "HASH_MISMATCH"
    INVALID_TICKER = "INVALID_TICKER"
    OUT_OF_BOUNDS = "OUT_OF_BOUNDS"
    MALFORMED_BUNDLE = "MALFORMED_BUNDLE"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    RATE_LIMITED = "RATE_LIMITED"
    ROUND_UNAVAILABLE = "ROUND_UNAVAILABLE"


class SubmitCommit(bt.Synapse):
    """Commit-window message: hash now, contents later."""

    round_id: str
    schema_id: str
    spec_version: int
    bundle_hash: str

    accepted: bool = False
    rejection_code: str | None = None


class SubmitReveal(bt.Synapse):
    """Reveal-window message: canonical bundle JSON + commit nonce."""

    round_id: str
    schema_id: str
    spec_version: int
    bundle_json: str
    nonce_hex: str

    accepted: bool = False
    rejection_code: str | None = None
