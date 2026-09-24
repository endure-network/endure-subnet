from __future__ import annotations

from decimal import Decimal

import pytest

from endure.protocol.consensus_policy import (
    MAINNET_GENESIS_HASH,
    SN30_NETUID,
    SN30_OWNER_HOTKEY,
    TESTNET_GENESIS_HASH,
    OwnerVoteNetwork,
)
from endure.scoring.emission_policy import (
    ChainSnapshot,
    EmissionBlocked,
    OwnerVoteRecipient,
    owner_vote_weights,
    plan_emission,
    recheck_owner_vote,
    resolve_owner_vote_uid,
    select_emission_mode,
    submission_due,
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
            chain_identity=(
                MAINNET_GENESIS_HASH if network == "mainnet" else TESTNET_GENESIS_HASH
            ),
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
    with pytest.raises(EmissionBlocked) as blocked:
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
        with pytest.raises(EmissionBlocked) as blocked:
            owner_vote_weights(size, uid)
        assert blocked.value.reason == "owner_snapshot_inconsistent"


@pytest.mark.parametrize(
    ("permit", "last_update", "block", "rate_limit", "expected"),
    [
        (True, 100, 279, 180, False),
        (True, 100, 280, 180, False),
        (True, 100, 281, 180, True),
        (False, 100, 281, 180, False),
        (True, 100, 100, 0, False),
        (True, 100, 101, 0, True),
    ],
)
def test_submission_obeys_permit_and_strict_chain_rate_limit(
    permit: bool, last_update: int, block: int, rate_limit: int, expected: bool
) -> None:
    assert (
        submission_due(
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
    with pytest.raises(EmissionBlocked) as blocked:
        submission_due(
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
        else "chain_snapshot_inconsistent"
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
def test_recheck_refuses_padding_redirection_or_incompatible_constraints(
    uids: list[int],
    weights: list[int],
    minimum: int | None,
    maximum: Decimal | None,
) -> None:
    hotkeys = ["other"] * 177
    hotkeys[176] = SN30_OWNER_HOTKEY
    recipient = OwnerVoteRecipient("mainnet", SN30_OWNER_HOTKEY, 176)

    def recheck(
        uids: list[int],
        weights: list[int],
        minimum: int | None,
        maximum: Decimal | None,
    ) -> None:
        recheck_owner_vote(
            recipient,
            chain_identity=MAINNET_GENESIS_HASH,
            netuid=SN30_NETUID,
            hotkeys=hotkeys,
            uint_uids=uids,
            uint_weights=weights,
            min_allowed_weights=minimum,
            max_weight_limit=maximum,
        )

    recheck([176], [65535], 1, Decimal(1))
    with pytest.raises(EmissionBlocked) as blocked:
        recheck(uids, weights, minimum, maximum)
    assert blocked.value.reason == "owner_vote_vector_invalid"


def test_recheck_refuses_an_owner_that_moved_in_the_prepared_metagraph() -> None:
    hotkeys = ["other"] * 177
    hotkeys[5] = SN30_OWNER_HOTKEY
    with pytest.raises(EmissionBlocked) as blocked:
        recheck_owner_vote(
            OwnerVoteRecipient("mainnet", SN30_OWNER_HOTKEY, 176),
            chain_identity=MAINNET_GENESIS_HASH,
            netuid=SN30_NETUID,
            hotkeys=hotkeys,
            uint_uids=[176],
            uint_weights=[65535],
            min_allowed_weights=1,
            max_weight_limit=Decimal(1),
        )
    assert blocked.value.reason == "owner_snapshot_inconsistent"


def test_testnet_owner_vote_is_impossible_on_the_mainnet_genesis() -> None:
    with pytest.raises(EmissionBlocked) as blocked:
        resolve_owner_vote_uid(
            network="testnet",
            chain_identity=MAINNET_GENESIS_HASH,
            netuid=417,
            hotkeys=["owner"],
            owner_hotkey="owner",
        )
    assert blocked.value.reason == "owner_vote_chain_mismatch"


def _snapshot(*, block: int = 1000, last_update: int = 800) -> ChainSnapshot:
    return ChainSnapshot(
        block=block,
        hotkeys=["validator", "miner", SN30_OWNER_HOTKEY],
        owner_hotkey=SN30_OWNER_HOTKEY,
        validator_permit=[True, False, False],
        last_update=[last_update, 0, 0],
        weights_rate_limit=180,
    )


@pytest.mark.parametrize("mode", ["scored", "owner_vote"])
@pytest.mark.parametrize(("last_update", "due"), [(820, False), (819, True)])
def test_both_modes_plan_the_strict_rate_limit_from_one_snapshot(
    mode: str, last_update: int, due: bool
) -> None:
    scores = [Decimal(0), Decimal("0.5"), Decimal(0)]
    plan = plan_emission(
        mode="scored" if mode == "scored" else "owner_vote",
        network="mainnet",
        snapshot=_snapshot(last_update=last_update),
        block=1000,
        chain_identity=MAINNET_GENESIS_HASH,
        netuid=SN30_NETUID,
        validator_uid=0,
        validator_hotkey="validator",
        local_hotkeys=["validator", "miner", SN30_OWNER_HOTKEY],
        scores=scores,
    )

    assert plan.due is due
    assert plan.next_eligible_block == last_update + 181
    if mode == "scored":
        assert plan.weights == tuple(scores) and plan.recipient is None
    else:
        assert plan.weights == (Decimal(0), Decimal(0), Decimal(1))
        assert plan.recipient == OwnerVoteRecipient("mainnet", SN30_OWNER_HOTKEY, 2)


@pytest.mark.parametrize("snapshot", [None, _snapshot(block=999)])
def test_missing_or_stale_snapshot_blocks_either_mode(
    snapshot: ChainSnapshot | None,
) -> None:
    with pytest.raises(EmissionBlocked) as blocked:
        plan_emission(
            mode="scored",
            network=None,
            snapshot=snapshot,
            block=1000,
            chain_identity="0xlocal",
            netuid=1,
            validator_uid=0,
            validator_hotkey="validator",
            local_hotkeys=["validator"],
            scores=[Decimal(1)],
        )
    assert blocked.value.reason == "chain_snapshot_inconsistent"
