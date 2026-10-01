from __future__ import annotations

import dataclasses
import random
import re
from decimal import Decimal

import pytest
from bittensor.core.chain_data.metagraph_info import (
    MetagraphInfo,
    SelectiveMetagraphIndex,
)

from endure.protocol.consensus_policy import (
    MAINNET_GENESIS_HASH,
    SN30_NETUID,
    SN30_OWNER_HOTKEY,
    TESTNET_GENESIS_HASH,
    OwnerVoteNetwork,
)
from endure.scoring.emission_policy import (
    CHAIN_SNAPSHOT_METAGRAPH_INDICES,
    FULL_BURN_BPS,
    ChainSnapshot,
    EmissionBlocked,
    OwnerCommitment,
    OwnerState,
    OwnerVoteRecipient,
    burn_commitment_text,
    chain_withheld_uids,
    commitment_text,
    observed_burn_rate,
    owner_burn_weights,
    owner_vote_weights,
    parse_burn_rate,
    plan_emission,
    recheck_owner_state,
    recheck_owner_vote,
    resolve_owner_vote_uid,
    select_emission_mode,
    submission_due,
)
from endure.scoring.weight_processing import chain_weight_vector, emission_candidate


def commitment_record(text: str) -> dict[str, object]:
    """A ``Commitments.CommitmentOf`` record in the shape the SDK decodes."""
    data = text.encode("utf-8")
    return {
        "deposit": 0,
        "block": 1,
        "info": {"fields": [{f"Raw{len(data)}": "0x" + data.hex()}]},
    }


NO_BURN = commitment_record("endure.burn_bps=0")
OWNER_COLDKEY = "owner-coldkey"


def test_selective_metagraph_indices_populate_exactly_the_snapshot_fields() -> None:
    # Enum members name the MetagraphInfo field they populate in CamelCase.
    selected = {
        re.sub(r"(?<!^)(?=[A-Z])", "_", SelectiveMetagraphIndex(index).name).lower()
        for index in CHAIN_SNAPSHOT_METAGRAPH_INDICES
    }
    snapshot_fields = {field.name for field in dataclasses.fields(ChainSnapshot)}

    assert selected == snapshot_fields | {"netuid"}
    assert selected <= {field.name for field in dataclasses.fields(MetagraphInfo)}


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
        owner_coldkey=OWNER_COLDKEY,
        coldkeys=["ck-validator", "ck-miner", OWNER_COLDKEY],
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
        owner_commitment=OwnerCommitment(SN30_OWNER_HOTKEY, 1000, NO_BURN),
    )

    assert plan.due is due
    assert plan.next_eligible_block == last_update + 181
    if mode == "scored":
        assert plan.weights == tuple(scores) and plan.burn_bps == 0
        assert plan.recipient is not None and plan.recipient.burn_bps == 0
    else:
        assert plan.weights == (Decimal(0), Decimal(0), Decimal(1))
        assert plan.recipient == OwnerVoteRecipient(
            "mainnet",
            SN30_OWNER_HOTKEY,
            2,
            FULL_BURN_BPS,
            OWNER_COLDKEY,
            frozenset({2}),
        )
        assert plan.burn_bps == FULL_BURN_BPS


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


def test_scored_mode_refuses_a_scored_uid_that_changed_hands_on_chain() -> None:
    snapshot = ChainSnapshot(
        block=1000,
        hotkeys=["validator", "new-registrant", "miner-2", SN30_OWNER_HOTKEY],
        owner_hotkey=SN30_OWNER_HOTKEY,
        owner_coldkey=OWNER_COLDKEY,
        coldkeys=["ck-validator", "ck-new", "ck-2", OWNER_COLDKEY],
        validator_permit=[True, False, False, False],
        last_update=[0, 0, 0, 0],
        weights_rate_limit=180,
    )

    def plan(scores: list[Decimal]) -> None:
        plan_emission(
            mode="scored",
            network="mainnet",
            snapshot=snapshot,
            block=1000,
            chain_identity=MAINNET_GENESIS_HASH,
            netuid=SN30_NETUID,
            validator_uid=0,
            validator_hotkey="validator",
            local_hotkeys=["validator", "miner-1", "miner-2", SN30_OWNER_HOTKEY],
            scores=scores,
            owner_commitment=OwnerCommitment(SN30_OWNER_HOTKEY, 1000, NO_BURN),
        )

    # A zero score on the changed UID sends it nothing and is fine.
    plan([Decimal(0), Decimal(0), Decimal("0.5"), Decimal(0)])
    with pytest.raises(EmissionBlocked) as blocked:
        plan([Decimal(0), Decimal("0.5"), Decimal("0.5"), Decimal(0)])
    assert blocked.value.reason == "chain_snapshot_inconsistent"


# Owner burn rate: the owner hotkey's commitment sets the owner share of a
# scored vote; anything but an exact commitment burns the whole vote.

BURN_HOTKEYS = ("validator", "miner-a", "miner-b", SN30_OWNER_HOTKEY)
BURN_SCORES = (Decimal(0), Decimal("0.5"), Decimal("0.25"), Decimal(0))


@pytest.mark.parametrize("burn_bps", [0, 1, 9800, FULL_BURN_BPS])
def test_published_burn_rate_round_trips_through_a_chain_record(burn_bps: int) -> None:
    text = burn_commitment_text(burn_bps)

    assert text == f"endure.burn_bps={burn_bps}"
    assert parse_burn_rate(commitment_text(commitment_record(text))) == burn_bps


@pytest.mark.parametrize("burn_bps", [-1, FULL_BURN_BPS + 1, True])
def test_owner_tooling_refuses_an_unpublishable_burn_rate(burn_bps: int) -> None:
    with pytest.raises(ValueError):
        burn_commitment_text(burn_bps)


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "endure.burn_bps=",
        "endure.burn_bps=10001",
        "endure.burn_bps=99999",
        "endure.burn_bps=100000",
        "endure.burn_bps=-1",
        "endure.burn_bps=+5",
        "endure.burn_bps=09800",
        "endure.burn_bps= 9800",
        " endure.burn_bps=9800",
        "endure.burn_bps=9800\n",
        "ENDURE.BURN_BPS=9800",
        "endure.burn_bps=1e3",
        "endure.burn_bps=9800.0",
        "endure.burn_bps=\uff19\uff18\uff10\uff10",
        "endure.burn_bps=0;endure.burn_bps=0",
        "burn_bps=0",
    ],
)
def test_anything_but_an_exact_commitment_burns_the_whole_vote(
    text: str | None,
) -> None:
    assert parse_burn_rate(text) == FULL_BURN_BPS


@pytest.mark.parametrize(
    "record",
    [
        "unexpected",
        {},
        {"info": None},
        {"info": {"fields": []}},
        {"info": {"fields": ["ResetBondsFlag"]}},
        {"info": {"fields": [{"Raw2": "0x6869"}, {"Raw2": "0x6869"}]}},
        {"info": {"fields": [{"Raw2": "0x6869", "Raw1": "0x68"}]}},
        {"info": {"fields": [{"Raw3": "0x6869"}]}},
        {"info": {"fields": [{"Raw2": "0xzz69"}]}},
        {"info": {"fields": [{"Raw2": "0xc328"}]}},
        {"info": {"fields": [{"Raw2": 26729}]}},
        {"info": {"fields": [{"Sha256": "0x" + "00" * 32}]}},
    ],
)
def test_a_commitment_record_is_one_exact_utf8_raw_field_or_malformed(
    record: object,
) -> None:
    assert (
        commitment_text(commitment_record("endure.burn_bps=42")) == "endure.burn_bps=42"
    )
    assert commitment_text(record) == ""


@pytest.mark.parametrize("absent", [None, ""])
def test_no_commitment_record_reads_as_absent(absent: object) -> None:
    # The SDK reports a missing CommitmentOf entry as an empty string.
    assert commitment_text(absent) is None
    assert parse_burn_rate(commitment_text(absent)) == FULL_BURN_BPS


def test_burn_gives_the_owner_its_rate_and_miners_the_earned_rest() -> None:
    # The owner's own score never adds to its share; negatives earn nothing.
    scores = [Decimal("-1"), Decimal("0.5"), Decimal("0.25"), Decimal("0.9")]
    weights = owner_burn_weights(scores, owner_uid=3, burn_bps=9800)

    assert weights is not None
    assert weights[3] == Decimal("0.98")
    assert weights[0] == 0
    assert weights[1] == 2 * weights[2]
    assert abs(sum(weights) - 1) < Decimal("1E-26")


def test_burn_without_another_positive_score_defers_to_the_owner_vote() -> None:
    scores = [Decimal(0), Decimal("-0.5"), Decimal(0), Decimal(1)]

    assert owner_burn_weights(scores, owner_uid=3, burn_bps=9800) is None
    with pytest.raises(EmissionBlocked) as blocked:
        owner_burn_weights(scores, owner_uid=4, burn_bps=9800)
    assert blocked.value.reason == "owner_snapshot_inconsistent"


def _burn_snapshot(
    owner_hotkey: str = SN30_OWNER_HOTKEY,
    coldkeys: tuple[str, ...] = ("ck-validator", "ck-a", "ck-b", OWNER_COLDKEY),
) -> ChainSnapshot:
    hotkeys = [*BURN_HOTKEYS[:-1], owner_hotkey]
    return ChainSnapshot(
        block=1000,
        hotkeys=hotkeys,
        owner_hotkey=owner_hotkey,
        owner_coldkey=OWNER_COLDKEY,
        coldkeys=coldkeys,
        validator_permit=[True, False, False, False],
        last_update=[800, 0, 0, 0],
        weights_rate_limit=180,
    )


def _plan_scored(
    record: object,
    *,
    scores: tuple[Decimal, ...] = BURN_SCORES,
    network: OwnerVoteNetwork | None = "mainnet",
    snapshot: ChainSnapshot | None = None,
):
    """Plan a scored attempt whose owner commitment read returned ``record``.

    ``None`` models an attempt that read no commitment at all.
    """
    snapshot = snapshot or _burn_snapshot()
    commitment = (
        None
        if record is None or snapshot.owner_hotkey is None
        else OwnerCommitment(snapshot.owner_hotkey, snapshot.block, record)
    )
    return plan_emission(
        mode="scored",
        network=network,
        snapshot=snapshot,
        block=1000,
        chain_identity=MAINNET_GENESIS_HASH,
        netuid=SN30_NETUID,
        validator_uid=0,
        validator_hotkey="validator",
        local_hotkeys=list(snapshot.hotkeys),
        scores=scores,
        owner_commitment=commitment,
    )


def test_a_scored_plan_applies_the_published_burn_rate() -> None:
    plan = _plan_scored(commitment_record("endure.burn_bps=9800"))

    assert plan.burn_bps == 9800
    assert plan.recipient == OwnerVoteRecipient(
        "mainnet", SN30_OWNER_HOTKEY, 3, 9800, OWNER_COLDKEY, frozenset({3})
    )
    assert plan.weights == owner_burn_weights(BURN_SCORES, owner_uid=3, burn_bps=9800)


@pytest.mark.parametrize(
    "commitment",
    [
        "",
        commitment_record("endure.burn_bps=9800\n"),
        {"deposit": 0, "block": 1, "info": {"fields": ["ResetBondsFlag"]}},
        commitment_record(burn_commitment_text(FULL_BURN_BPS)),
    ],
)
def test_a_scored_plan_without_a_usable_burn_rate_is_the_owner_vote(
    commitment: object,
) -> None:
    plan = _plan_scored(commitment)

    assert plan.burn_bps == FULL_BURN_BPS
    assert plan.weights == owner_vote_weights(len(BURN_HOTKEYS), 3)
    assert plan.recipient is not None and plan.recipient.burn_bps == FULL_BURN_BPS


def test_a_zero_burn_rate_keeps_the_earned_vector_unchanged() -> None:
    plan = _plan_scored(NO_BURN)

    assert plan.burn_bps == 0
    assert plan.weights == BURN_SCORES
    # The owner context stays attached so the recheck still runs at 0%.
    assert plan.recipient == OwnerVoteRecipient(
        "mainnet", SN30_OWNER_HOTKEY, 3, 0, OWNER_COLDKEY, frozenset({3})
    )


def test_development_chains_never_read_a_burn_rate() -> None:
    plan = _plan_scored(commitment_record("endure.burn_bps=9800"), network=None)

    assert plan.burn_bps is None
    assert plan.weights == BURN_SCORES
    assert plan.recipient is None


def test_a_burn_with_only_the_owner_scored_is_the_owner_vote() -> None:
    plan = _plan_scored(
        commitment_record("endure.burn_bps=9800"),
        scores=(Decimal(0), Decimal(0), Decimal(0), Decimal(1)),
    )

    assert plan.burn_bps == FULL_BURN_BPS
    assert plan.weights == owner_vote_weights(len(BURN_HOTKEYS), 3)


@pytest.mark.parametrize(
    "commitment", [NO_BURN, commitment_record("endure.burn_bps=9800")]
)
def test_scored_votes_on_vote_networks_require_the_pinned_owner(
    commitment: object,
) -> None:
    # The burn rate is only as trustworthy as the owner key that publishes it.
    with pytest.raises(EmissionBlocked) as blocked:
        _plan_scored(commitment, snapshot=_burn_snapshot("replacement-owner"))
    assert blocked.value.reason == "owner_hotkey_mismatch"


def _burn_vector(
    scores: list[Decimal], *, owner_uid: int, burn_bps: int
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    burned = owner_burn_weights(scores, owner_uid=owner_uid, burn_bps=burn_bps)
    assert burned is not None
    raw = emission_candidate(burned)
    assert raw is not None
    vector = chain_weight_vector(
        raw,
        uids=list(range(len(scores))),
        metagraph_size=len(scores),
        min_allowed_weights=1,
        max_weight_limit=Decimal(1),
    )
    return vector.uint_uids, vector.uint_weights


def test_the_recheck_accepts_every_burn_vector_the_pipeline_encodes() -> None:
    rng = random.Random(2043)  # noqa: S311 — a seeded case generator, not secrecy
    for _ in range(200):
        size = rng.randint(2, 256)
        owner_uid = rng.randrange(size)
        scores = [
            Decimal(rng.randint(1, 10**6)) / Decimal(10**6)
            if rng.random() < 0.5
            else Decimal(0)
            for _ in range(size)
        ]
        miner = rng.choice([uid for uid in range(size) if uid != owner_uid])
        scores[miner] = Decimal(rng.randint(1, 10**6)) / Decimal(10**6)
        burn_bps = rng.choice([1, 50, 5000, 9800, 9999, rng.randint(1, 9999)])
        hotkeys = [f"hk-{uid}" for uid in range(size)]
        hotkeys[owner_uid] = SN30_OWNER_HOTKEY
        uids, weights = _burn_vector(scores, owner_uid=owner_uid, burn_bps=burn_bps)

        recheck_owner_vote(
            OwnerVoteRecipient("mainnet", SN30_OWNER_HOTKEY, owner_uid, burn_bps),
            chain_identity=MAINNET_GENESIS_HASH,
            netuid=SN30_NETUID,
            hotkeys=hotkeys,
            uint_uids=uids,
            uint_weights=weights,
            min_allowed_weights=1,
            max_weight_limit=Decimal(1),
        )


@pytest.mark.parametrize(
    ("uids", "weights", "minimum"),
    [
        ([1, 2], [65535, 32768], 1),
        ([3], [65535], 1),
        ([3, 3], [65535, 1337], 1),
        ([3, 1, 2], [65535, 891, 0], 1),
        ([3, 1, 2], [65535, 891], 1),
        ([3, 1, 2], [32768, 65535, 32768], 1),
        ("pipeline", "pipeline", 2),
    ],
)
def test_the_recheck_refuses_a_vector_that_misstates_the_burn(
    uids: list[int] | str, weights: list[int] | str, minimum: int
) -> None:
    def recheck(uids: list[int], weights: list[int], minimum: int) -> None:
        recheck_owner_vote(
            OwnerVoteRecipient("mainnet", SN30_OWNER_HOTKEY, 3, 9800),
            chain_identity=MAINNET_GENESIS_HASH,
            netuid=SN30_NETUID,
            hotkeys=BURN_HOTKEYS,
            uint_uids=uids,
            uint_weights=weights,
            min_allowed_weights=minimum,
            max_weight_limit=Decimal(1),
        )

    pipeline_uids, pipeline_weights = _burn_vector(
        list(BURN_SCORES), owner_uid=3, burn_bps=9800
    )
    recheck(list(pipeline_uids), list(pipeline_weights), 1)
    with pytest.raises(EmissionBlocked) as blocked:
        recheck(
            list(pipeline_uids) if isinstance(uids, str) else uids,
            list(pipeline_weights) if isinstance(weights, str) else weights,
            minimum,
        )
    assert blocked.value.reason == "owner_vote_vector_invalid"


def test_only_a_due_scored_vote_asks_for_the_owner_commitment() -> None:
    # Owner, permit and rate limit pass first; the plan has nothing to send.
    plan = _plan_scored(None)
    assert plan.commitment_owner == SN30_OWNER_HOTKEY
    assert plan.weights == () and plan.recipient is None and plan.burn_bps is None

    rate_limited = dataclasses.replace(_burn_snapshot(), last_update=[900, 0, 0, 0])
    assert _plan_scored(None, snapshot=rate_limited).commitment_owner is None
    assert _plan_scored(None, network=None).commitment_owner is None
    only_withheld = (Decimal(0), Decimal(0), Decimal(0), Decimal(1))
    assert _plan_scored(None, scores=only_withheld).commitment_owner is None
    owner_vote = plan_emission(
        mode="owner_vote",
        network="mainnet",
        snapshot=_burn_snapshot(),
        block=1000,
        chain_identity=MAINNET_GENESIS_HASH,
        netuid=SN30_NETUID,
        validator_uid=0,
        validator_hotkey="validator",
        local_hotkeys=list(BURN_HOTKEYS),
        scores=[Decimal(0)] * len(BURN_HOTKEYS),
    )
    assert owner_vote.commitment_owner is None


def test_every_hotkey_of_the_owner_coldkey_is_withheld() -> None:
    snapshot = _burn_snapshot(
        coldkeys=("ck-validator", OWNER_COLDKEY, "ck-b", OWNER_COLDKEY)
    )

    assert chain_withheld_uids(snapshot, 3) == frozenset({1, 3})
    for broken in (
        dataclasses.replace(snapshot, owner_coldkey=None),
        dataclasses.replace(snapshot, coldkeys=("ck-validator", OWNER_COLDKEY)),
    ):
        with pytest.raises(EmissionBlocked) as blocked:
            chain_withheld_uids(broken, 3)
        assert blocked.value.reason == "owner_snapshot_inconsistent"


@pytest.mark.parametrize("burn_bps", [0, 9800])
def test_a_scored_owner_coldkey_sibling_earns_nothing_at_any_rate(
    burn_bps: int,
) -> None:
    # The chain withholds a sibling's incentive, so its earned share would be
    # burned on top of the published rate.
    sibling = _burn_snapshot(
        coldkeys=("ck-validator", "ck-a", OWNER_COLDKEY, OWNER_COLDKEY)
    )
    scores = (Decimal(0), Decimal("0.5"), Decimal("0.5"), Decimal("0.9"))
    plan = _plan_scored(
        commitment_record(f"endure.burn_bps={burn_bps}"),
        scores=scores,
        snapshot=sibling,
    )

    earned_by_a = (Decimal(0), Decimal("0.5"), Decimal(0), Decimal(0))
    assert plan.recipient is not None
    assert plan.recipient.withheld == frozenset({2, 3})
    if burn_bps == 0:
        assert plan.weights == earned_by_a
    else:
        assert plan.weights == owner_burn_weights(
            earned_by_a, owner_uid=3, burn_bps=burn_bps
        )
        assert plan.weights[2] == 0 and plan.weights[3] == Decimal("0.98")
    assert plan.burn_bps == burn_bps


def test_only_withheld_uids_scoring_is_the_owner_vote() -> None:
    plan = _plan_scored(
        NO_BURN,
        scores=(Decimal(0), Decimal(0), Decimal("0.5"), Decimal("0.9")),
        snapshot=_burn_snapshot(
            coldkeys=("ck-validator", "ck-a", OWNER_COLDKEY, OWNER_COLDKEY)
        ),
    )

    assert plan.burn_bps == FULL_BURN_BPS
    assert plan.weights == owner_vote_weights(len(BURN_HOTKEYS), 3)


@pytest.mark.parametrize(
    ("commitment", "reason"),
    [
        (
            OwnerCommitment("another-hotkey", 1000, NO_BURN),
            "chain_snapshot_inconsistent",
        ),
        (
            OwnerCommitment(SN30_OWNER_HOTKEY, 999, NO_BURN),
            "chain_snapshot_inconsistent",
        ),
        (
            OwnerCommitment(SN30_OWNER_HOTKEY, 1000, {**NO_BURN, "block": 1001}),
            "chain_snapshot_inconsistent",
        ),
    ],
)
def test_a_commitment_read_must_describe_the_snapshot(
    commitment: OwnerCommitment, reason: str
) -> None:
    snapshot = _burn_snapshot()
    with pytest.raises(EmissionBlocked) as blocked:
        observed_burn_rate(commitment, snapshot)

    assert blocked.value.reason == reason
    assert (
        observed_burn_rate(
            OwnerCommitment(SN30_OWNER_HOTKEY, 1000, {**NO_BURN, "block": 1000}),
            snapshot,
        )
        == 0
    )
    assert observed_burn_rate(None, snapshot) == FULL_BURN_BPS


@pytest.mark.parametrize(
    ("uids", "weights"),
    [
        ([3, 1], [1, 1]),
        ([3, 1], [65535, 65535 * 2]),
        ([3, 1, 1], [65535, 668, 669]),
        ([3, 256], [65535, 1337]),
    ],
)
def test_the_burn_recheck_enforces_its_rounding_premises(
    uids: list[int], weights: list[int]
) -> None:
    # In a 256-UID metagraph the rounding tolerance alone would accept every
    # one of these (even a 50% owner share), so only the premises refuse them:
    # a max-scaled vector, entries within u16, unique UIDs inside the metagraph.
    hotkeys = [f"hk-{uid}" for uid in range(256)]
    hotkeys[3] = SN30_OWNER_HOTKEY

    def recheck(uids: list[int], weights: list[int]) -> None:
        recheck_owner_vote(
            OwnerVoteRecipient("mainnet", SN30_OWNER_HOTKEY, 3, 9800),
            chain_identity=MAINNET_GENESIS_HASH,
            netuid=SN30_NETUID,
            hotkeys=hotkeys,
            uint_uids=uids,
            uint_weights=weights,
            min_allowed_weights=1,
            max_weight_limit=Decimal(1),
        )

    recheck([3, 1], [65535, 1337])
    with pytest.raises(EmissionBlocked) as blocked:
        recheck(uids, weights)
    assert blocked.value.reason == "owner_vote_vector_invalid"


@pytest.mark.parametrize(
    ("burn_bps", "uids", "weights"),
    [
        (0, [1, 3], [65535, 100]),
        (0, [1, 2], [65535, 100]),
        (9800, [1, 2, 3], [668, 669, 65535]),
    ],
)
def test_the_recheck_refuses_earned_weight_on_a_withheld_uid(
    burn_bps: int, uids: list[int], weights: list[int]
) -> None:
    # UID 2 shares the owner coldkey; chain padding or a stale plan could put
    # weight on it or on the owner even at a zero rate.
    recipient = OwnerVoteRecipient(
        "mainnet", SN30_OWNER_HOTKEY, 3, burn_bps, OWNER_COLDKEY, frozenset({2, 3})
    )

    def recheck(minimum: int, uids: list[int], weights: list[int]) -> None:
        recheck_owner_vote(
            recipient,
            chain_identity=MAINNET_GENESIS_HASH,
            netuid=SN30_NETUID,
            hotkeys=BURN_HOTKEYS,
            uint_uids=uids,
            uint_weights=weights,
            min_allowed_weights=minimum,
            max_weight_limit=Decimal(1),
        )

    if burn_bps == 0:
        recheck(1, [1], [65535])
        with pytest.raises(EmissionBlocked) as padded:
            recheck(2, [1], [65535])
        assert padded.value.reason == "owner_vote_vector_invalid"
    with pytest.raises(EmissionBlocked) as blocked:
        recheck(1, uids, weights)
    assert blocked.value.reason == "owner_vote_vector_invalid"


def _owner_state(**changes: object) -> OwnerState:
    state = OwnerState(
        block=1001,
        owner_hotkey=SN30_OWNER_HOTKEY,
        owner_coldkey=OWNER_COLDKEY,
        hotkeys=BURN_HOTKEYS,
        coldkeys=("ck-validator", "ck-a", "ck-b", OWNER_COLDKEY),
    )
    return dataclasses.replace(state, **changes)


@pytest.mark.parametrize(
    ("state", "block", "reason"),
    [
        (None, 1001, "chain_snapshot_inconsistent"),
        (_owner_state(), 1002, "chain_snapshot_inconsistent"),
        (_owner_state(), None, "chain_snapshot_inconsistent"),
        (_owner_state(owner_hotkey="new-owner"), 1001, "owner_snapshot_inconsistent"),
        (_owner_state(owner_coldkey="buyer"), 1001, "owner_snapshot_inconsistent"),
        (
            _owner_state(
                hotkeys=("validator", "re-registered", "miner-b", SN30_OWNER_HOTKEY)
            ),
            1001,
            "chain_snapshot_inconsistent",
        ),
        (
            _owner_state(
                coldkeys=("ck-validator", OWNER_COLDKEY, "ck-b", OWNER_COLDKEY)
            ),
            1001,
            "owner_snapshot_inconsistent",
        ),
        (_owner_state(coldkeys=("ck-validator",)), 1001, "owner_snapshot_inconsistent"),
    ],
)
def test_the_owner_facts_a_vector_relies_on_must_hold_at_submission(
    state: OwnerState | None, block: int | None, reason: str
) -> None:
    recipient = OwnerVoteRecipient(
        "mainnet", SN30_OWNER_HOTKEY, 3, 9800, OWNER_COLDKEY, frozenset({3})
    )

    def recheck(state: OwnerState | None, block: int | None) -> None:
        recheck_owner_state(
            recipient,
            state,
            block=block,
            prepared_hotkeys=BURN_HOTKEYS,
            uint_uids=[1, 2, 3],
        )

    recheck(_owner_state(), 1001)
    with pytest.raises(EmissionBlocked) as blocked:
        recheck(state, block)
    assert blocked.value.reason == reason
