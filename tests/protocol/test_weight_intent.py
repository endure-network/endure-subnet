from __future__ import annotations

import pytest

from endure.protocol.weight_intent import (
    WeightIntentPayload,
    canonical_weight_intent_hash,
)


@pytest.mark.parametrize(
    ("targets", "expected"),
    [
        (
            ((1, "miner-hotkey", 65535),),
            "25:5eddab9f304ce333a37c4939c218b53fb8875e37792b3c1f6989ef7a3da1deb0",
        ),
        (
            (),
            "25:8e09a0771247756cf638a3a8837507204f592c8f16f2dcd271fd7addafb05aa4",
        ),
    ],
)
def test_canonical_weight_intent_matches_golden_vectors(
    targets: tuple[tuple[int, str, int], ...], expected: str
) -> None:
    intent = WeightIntentPayload(
        protocol_version_key=25,
        chain_identity="genesis-a",
        netuid=1,
        validator_uid=0,
        validator_hotkey="validator-hotkey",
        targets=targets,
    )

    assert canonical_weight_intent_hash(intent) == expected


def test_canonical_weight_intent_sorts_targets_before_hashing() -> None:
    intent = WeightIntentPayload(
        protocol_version_key=25,
        chain_identity="chain-z",
        netuid=42,
        validator_uid=9,
        validator_hotkey="val-z",
        targets=((7, "hk-a", 65535), (2, "hk-b", 100)),
    )

    assert canonical_weight_intent_hash(intent) == (
        "25:2639f2c39441b0f55f6c27c08fb63a8edf5923e7b1b88310eec86c5339e18bf2"
    )
