"""Canonical weight-emission intent identity (fairness-deltas spec §2)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WeightIntentPayload:
    protocol_version_key: int
    chain_identity: str
    netuid: int
    validator_uid: int
    validator_hotkey: str
    targets: tuple[tuple[int, str, int], ...]


def canonical_weight_intent_hash(intent: WeightIntentPayload) -> str:
    payload = json.dumps(
        {
            "chain_identity": intent.chain_identity,
            "netuid": intent.netuid,
            "version_key": intent.protocol_version_key,
            "targets": sorted(intent.targets),
            "validator_hotkey": intent.validator_hotkey,
            "validator_uid": intent.validator_uid,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    digest = hashlib.blake2b(payload, digest_size=32).hexdigest()
    return f"{intent.protocol_version_key}:{digest}"
