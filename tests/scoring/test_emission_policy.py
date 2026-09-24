from __future__ import annotations

from decimal import Decimal

import pytest

from endure.protocol.consensus_policy import (
    MAINNET_GENESIS_HASH,
    SN30_NETUID,
    SN30_OWNER_HOTKEY,
    OwnerVoteNetwork,
)
from endure.scoring.emission_policy import (
    OwnerVoteBlocked,
    owner_vote_submission_due,
    owner_vote_weights,
    resolve_owner_vote_uid,
    select_emission_mode,
    validate_owner_vote_vector,
)


@pytest.mark.parametrize("network", [None, "mainnet", "testnet"])
def test_any_positive_score_selects_earned_weights(
    network: OwnerVoteNetwork | None,
) -> None:
    scores = [Decimal("-0.2"), Decimal(0), Decimal("1E-999")]

    assert select_emission_mode(scores, owner_vote_network=network) == "scored"


@pytest.mark.parametrize(
    "scores",
    [[], [Decimal(0)] * 3, [Decimal("-1"), Decimal("0E-28")]],
)
@pytest.mark.parametrize(
    ("network", "expected"),
    [(None, "abstain"), ("mainnet", "owner_vote"), ("testnet", "owner_vote")],
)
def test_nonpositive_scores_fall_back_to_owner_vote_only_on_vote_networks(
    scores: list[Decimal], network: OwnerVoteNetwork | None, expected: str
) -> None:
    assert select_emission_mode(scores, owner_vote_network=network) == expected


@pytest.mark.parametrize("network", ["mainnet", "testnet"])
@pytest.mark.parametrize("owner_uid", [0, 7, 176])
def test_owner_resolves_to_its_current_uid(
    network: OwnerVoteNetwork, owner_uid: int
) -> None:
    hotkeys = [f"hk-{uid}" for uid in range(200)]
    hotkeys[owner_uid] = SN30_OWNER_HOTKEY

    assert (
        resolve_owner_vote_uid(
            network=network,
            chain_identity=MAINNET_GENESIS_HASH,
            netuid=SN30_NETUID,
            hotkeys=hotkeys,
            owner_hotkey=SN30_OWNER_HOTKEY,
        )
        == owner_uid
    )


def test_testnet_accepts_any_owner_hotkey_chain_and_netuid() -> None:
    assert (
        resolve_owner_vote_uid(
            network="testnet",
            chain_identity="testnet-genesis",
            netuid=417,
            hotkeys=["a", "testnet-owner", "b"],
            owner_hotkey="testnet-owner",
        )
        == 1
    )


@pytest.mark.parametrize(
    ("network", "chain_identity", "netuid", "hotkeys", "owner", "reason"),
    [
        (
            "mainnet",
            "testnet-genesis",
            SN30_NETUID,
            [SN30_OWNER_HOTKEY],
            SN30_OWNER_HOTKEY,
            "owner_vote_chain_mismatch",
        ),
        (
            "mainnet",
            MAINNET_GENESIS_HASH.upper(),
            SN30_NETUID,
            [SN30_OWNER_HOTKEY],
            SN30_OWNER_HOTKEY,
            "owner_vote_chain_mismatch",
        ),
        (
            "mainnet",
            MAINNET_GENESIS_HASH,
            31,
            [SN30_OWNER_HOTKEY],
            SN30_OWNER_HOTKEY,
            "owner_vote_chain_mismatch",
        ),
        (
            "mainnet",
            MAINNET_GENESIS_HASH,
            SN30_NETUID,
            ["replacement"],
            "replacement",
            "owner_hotkey_mismatch",
        ),
        (
            "mainnet",
            MAINNET_GENESIS_HASH,
            SN30_NETUID,
            ["other"],
            SN30_OWNER_HOTKEY,
            "owner_unregistered",
        ),
        ("testnet", "g", 1, ["other"], "owner", "owner_unregistered"),
        ("testnet", "g", 1, ["owner"], None, "owner_snapshot_inconsistent"),
        ("testnet", "g", 1, ["owner"], "", "owner_snapshot_inconsistent"),
        ("testnet", "g", 1, ["owner", "owner"], "owner", "owner_snapshot_inconsistent"),
    ],
)
def test_owner_resolution_blocks_with_a_stable_reason(
    network: OwnerVoteNetwork,
    chain_identity: str,
    netuid: int,
    hotkeys: list[str],
    owner: str | None,
    reason: str,
) -> None:
    with pytest.raises(OwnerVoteBlocked) as blocked:
        resolve_owner_vote_uid(
            network=network,
            chain_identity=chain_identity,
            netuid=netuid,
            hotkeys=hotkeys,
            owner_hotkey=owner,
        )

    assert blocked.value.reason == reason


def test_owner_vote_weights_are_one_hot_within_the_local_metagraph() -> None:
    assert owner_vote_weights(3, 2) == (Decimal(0), Decimal(0), Decimal(1))
    for size, uid in ((3, 3), (0, 0), (3, -1)):
        with pytest.raises(OwnerVoteBlocked) as blocked:
            owner_vote_weights(size, uid)
        assert blocked.value.reason == "owner_snapshot_inconsistent"


@pytest.mark.parametrize(
    ("permit", "last_update", "block", "rate_limit", "expected"),
    [
        (True, 100, 279, 180, False),
        (True, 100, 280, 180, True),
        (True, 100, 281, 180, True),
        (False, 100, 280, 180, False),
        (True, 100, 100, 0, True),
    ],
)
def test_submission_obeys_permit_and_inclusive_rate_boundary(
    permit: bool, last_update: int, block: int, rate_limit: int, expected: bool
) -> None:
    assert (
        owner_vote_submission_due(
            validator_uid=1,
            validator_hotkey="validator",
            hotkeys=["other", "validator"],
            validator_permits=[False, permit],
            last_updates=[0, last_update],
            block=block,
            weights_rate_limit=rate_limit,
        )
        is expected
    )


@pytest.mark.parametrize(
    ("uid", "hotkey", "permits", "updates", "block", "rate_limit", "reason"),
    [
        (-1, "validator", [False, True], [0, 100], 280, 180, "identity"),
        (2, "validator", [False, True], [0, 100], 280, 180, "identity"),
        (0, "validator", [False, True], [0, 100], 280, 180, "identity"),
        (1, "", [False, True], [0, 100], 280, 180, "identity"),
        (1, "validator", [False], [0, 100], 280, 180, "snapshot"),
        (1, "validator", [False, True], [0], 280, 180, "snapshot"),
        (1, "validator", [False, True], [0, 100], -1, 180, "snapshot"),
        (1, "validator", [False, True], [0, 100], 280, -1, "snapshot"),
        (1, "validator", [False, True], [0, -1], 280, 180, "snapshot"),
        (1, "validator", [False, False], [0, 281], 280, 180, "snapshot"),
    ],
)
def test_submission_rejects_identity_or_incoherent_metadata(  # noqa: PLR0913
    uid: int,
    hotkey: str,
    permits: list[bool],
    updates: list[int],
    block: int,
    rate_limit: int,
    reason: str,
) -> None:
    with pytest.raises(OwnerVoteBlocked) as blocked:
        owner_vote_submission_due(
            validator_uid=uid,
            validator_hotkey=hotkey,
            hotkeys=["other", "validator"],
            validator_permits=permits,
            last_updates=updates,
            block=block,
            weights_rate_limit=rate_limit,
        )

    assert blocked.value.reason == (
        "validator_identity_invalid"
        if reason == "identity"
        else "owner_snapshot_inconsistent"
    )


@pytest.mark.parametrize(
    ("uids", "weights", "minimum", "maximum"),
    [
        ([], [], 1, Decimal(1)),
        ([175], [65535], 1, Decimal(1)),
        ([176], [65534], 1, Decimal(1)),
        ([176, 177], [65535, 0], 1, Decimal(1)),
        ([176, 176], [65535, 65535], 1, Decimal(1)),
        ([176], [65535, 0], 1, Decimal(1)),
        ([176], [65535], None, Decimal(1)),
        ([176], [65535], 0, Decimal(1)),
        ([176], [65535], 2, Decimal(1)),
        ([176], [65535], 1, None),
        ([176], [65535], 1, Decimal("0.5")),
        ([176], [65535], 1, Decimal(2)),
        ([176], [65535], 1, Decimal("NaN")),
        ([176], [65535], 1, Decimal("sNaN")),
        ([176], [65535], 1, Decimal("Infinity")),
    ],
)
def test_vector_validation_refuses_padding_redirection_or_incompatible_constraints(
    uids: list[int],
    weights: list[int],
    minimum: int | None,
    maximum: Decimal | None,
) -> None:
    validate_owner_vote_vector(
        owner_uid=176,
        uids=[176],
        weights=[65535],
        min_allowed_weights=1,
        max_weight_limit=Decimal(1),
    )

    with pytest.raises(OwnerVoteBlocked) as blocked:
        validate_owner_vote_vector(
            owner_uid=176,
            uids=uids,
            weights=weights,
            min_allowed_weights=minimum,
            max_weight_limit=maximum,
        )
    assert blocked.value.reason == "owner_vote_vector_invalid"
