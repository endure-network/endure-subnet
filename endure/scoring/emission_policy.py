"""Pure, release-pinned emission planning: earned weights or the owner vote.

When no miner holds a positive score, a validator on an owner-vote network
assigns its whole vote to the on-chain subnet owner instead of abstaining.
This is a standing fallback, not a one-time bootstrap: it applies at cold
start and again whenever every scored miner has been archived. The owner
allocation never enters scores or EMAs.

Both modes plan from one chain snapshot: validator identity, permit and the
chain's strict weights rate limit decide whether an attempt is due, so a
rate-limited attempt is deferred instead of being refused by the SDK and
recorded as a failed submission.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, NamedTuple

from endure.protocol.consensus_policy import (
    MAINNET_GENESIS_HASH,
    SN30_NETUID,
    SN30_OWNER_HOTKEY,
    OwnerVoteNetwork,
)
from endure.scoring.weight_processing import U16_MAX

EmissionMode = Literal["scored", "owner_vote", "abstain"]
EmissionBlockReason = Literal[
    "owner_vote_chain_mismatch",
    "owner_hotkey_mismatch",
    "owner_unregistered",
    "owner_snapshot_inconsistent",
    "chain_snapshot_inconsistent",
    "validator_identity_invalid",
    "owner_vote_vector_invalid",
]


class EmissionBlocked(ValueError):
    """The chain state cannot safely support this emission attempt."""

    def __init__(self, reason: EmissionBlockReason, detail: str) -> None:
        super().__init__(detail)
        self.reason: EmissionBlockReason = reason


class OwnerVoteRecipient(NamedTuple):
    """Owner resolved for one attempt, rechecked against its prepared vector."""

    network: OwnerVoteNetwork
    hotkey: str
    uid: int


@dataclass(frozen=True, slots=True)
class ChainSnapshot:
    """The chain facts one emission attempt is planned from."""

    block: int
    hotkeys: Sequence[str]
    owner_hotkey: str | None
    validator_permit: Sequence[bool]
    last_update: Sequence[int]
    weights_rate_limit: int


@dataclass(frozen=True, slots=True)
class EmissionPlan:
    due: bool
    permit: bool
    next_eligible_block: int
    weights: tuple[Decimal, ...]
    recipient: OwnerVoteRecipient | None


def select_emission_mode(
    scores: Sequence[Decimal], *, owner_vote_network: OwnerVoteNetwork | None
) -> EmissionMode:
    """Earned weights whenever any score is positive; otherwise the fallback."""
    if any(score > 0 for score in scores):
        return "scored"
    return "abstain" if owner_vote_network is None else "owner_vote"


def plan_emission(  # noqa: PLR0913 — one snapshot plus the attempt's identity
    *,
    mode: Literal["scored", "owner_vote"],
    network: OwnerVoteNetwork | None,
    snapshot: ChainSnapshot | None,
    block: int,
    chain_identity: str,
    netuid: int,
    validator_uid: int,
    validator_hotkey: str,
    local_hotkeys: Sequence[str],
    scores: Sequence[Decimal],
) -> EmissionPlan:
    """Plan one attempt from one snapshot, or raise ``EmissionBlocked``."""
    if snapshot is None or snapshot.block != block:
        raise EmissionBlocked(
            "chain_snapshot_inconsistent", "No chain snapshot at this block"
        )
    recipient: OwnerVoteRecipient | None = None
    weights = tuple(scores)
    if mode == "owner_vote":
        if network is None:
            raise EmissionBlocked(
                "owner_vote_chain_mismatch", "Owner vote requires a vote network"
            )
        owner_uid = resolve_owner_vote_uid(
            network=network,
            chain_identity=chain_identity,
            netuid=netuid,
            hotkeys=snapshot.hotkeys,
            owner_hotkey=snapshot.owner_hotkey,
        )
        # The emitter indexes the local metagraph; it must place the owner at
        # the same UID as the chain snapshot or the vote goes elsewhere.
        if (
            owner_uid >= len(local_hotkeys)
            or local_hotkeys[owner_uid] != snapshot.owner_hotkey
        ):
            raise EmissionBlocked(
                "owner_snapshot_inconsistent",
                "Local metagraph disagrees with the chain owner UID",
            )
        weights = owner_vote_weights(len(local_hotkeys), owner_uid)
        recipient = OwnerVoteRecipient(network, local_hotkeys[owner_uid], owner_uid)
    due = submission_due(
        validator_uid=validator_uid,
        validator_hotkey=validator_hotkey,
        hotkeys=snapshot.hotkeys,
        validator_permits=snapshot.validator_permit,
        last_updates=snapshot.last_update,
        block=block,
        weights_rate_limit=snapshot.weights_rate_limit,
    )
    return EmissionPlan(
        due=due,
        permit=bool(snapshot.validator_permit[validator_uid]),
        next_eligible_block=(
            snapshot.last_update[validator_uid] + snapshot.weights_rate_limit + 1
        ),
        weights=weights,
        recipient=recipient,
    )


def resolve_owner_vote_uid(
    *,
    network: OwnerVoteNetwork,
    chain_identity: str,
    netuid: int,
    hotkeys: Sequence[str],
    owner_hotkey: str | None,
) -> int:
    """Resolve the on-chain subnet owner to its UID in one metagraph snapshot."""
    if network == "mainnet":
        if chain_identity != MAINNET_GENESIS_HASH or netuid != SN30_NETUID:
            raise EmissionBlocked(
                "owner_vote_chain_mismatch", "Mainnet owner vote is pinned to SN30"
            )
        if owner_hotkey != SN30_OWNER_HOTKEY:
            raise EmissionBlocked(
                "owner_hotkey_mismatch", "Subnet owner is not the pinned SN30 owner"
            )
    elif chain_identity == MAINNET_GENESIS_HASH:
        # Defense in depth: an unpinned testnet vote must never reach mainnet.
        raise EmissionBlocked(
            "owner_vote_chain_mismatch", "Testnet owner vote on the mainnet chain"
        )
    if not owner_hotkey:
        raise EmissionBlocked(
            "owner_snapshot_inconsistent", "Snapshot has no subnet owner hotkey"
        )
    matches = [uid for uid, hotkey in enumerate(hotkeys) if hotkey == owner_hotkey]
    if not matches:
        raise EmissionBlocked("owner_unregistered", "Subnet owner is not registered")
    if len(matches) != 1:
        raise EmissionBlocked(
            "owner_snapshot_inconsistent", "Subnet owner appears at multiple UIDs"
        )
    return matches[0]


def owner_vote_weights(size: int, owner_uid: int) -> tuple[Decimal, ...]:
    """One-hot raw vector over the local metagraph used for the attempt."""
    if not 0 <= owner_uid < size:
        raise EmissionBlocked(
            "owner_snapshot_inconsistent", "Owner UID is outside the local metagraph"
        )
    return tuple(Decimal(1) if uid == owner_uid else Decimal(0) for uid in range(size))


def submission_due(  # noqa: PLR0913 — explicit chain snapshot and identity
    *,
    validator_uid: int,
    validator_hotkey: str,
    hotkeys: Sequence[str],
    validator_permits: Sequence[bool],
    last_updates: Sequence[int],
    block: int,
    weights_rate_limit: int,
) -> bool:
    """Check validator identity, permit, and the chain's strict rate limit.

    Subtensor accepts ``set_weights`` only once ``block - last_update`` exceeds
    ``weights_rate_limit``; at equality the SDK refuses the extrinsic.
    """
    if validator_uid < 0 or validator_uid >= len(hotkeys):
        raise EmissionBlocked(
            "validator_identity_invalid", "Validator UID is not registered"
        )
    if not validator_hotkey or hotkeys[validator_uid] != validator_hotkey:
        raise EmissionBlocked(
            "validator_identity_invalid", "Validator UID does not match its hotkey"
        )
    if validator_uid >= len(validator_permits) or validator_uid >= len(last_updates):
        raise EmissionBlocked(
            "chain_snapshot_inconsistent", "Validator metadata is missing"
        )
    last_update = last_updates[validator_uid]
    if block < 0 or weights_rate_limit < 0 or last_update < 0 or last_update > block:
        raise EmissionBlocked(
            "chain_snapshot_inconsistent", "Incoherent snapshot block or rate data"
        )
    return validator_permits[validator_uid] and block - last_update > weights_rate_limit


def recheck_owner_vote(  # noqa: PLR0913 — the prepared attempt's chain facts
    recipient: OwnerVoteRecipient,
    *,
    chain_identity: str,
    netuid: int,
    hotkeys: Sequence[str],
    uint_uids: Sequence[int],
    uint_weights: Sequence[int],
    min_allowed_weights: int | None,
    max_weight_limit: Decimal | None,
) -> None:
    """Recheck the planned owner against the exact prepared vector.

    This re-resolves the snapshot's owner hotkey in the metagraph and chain
    identity the vector was prepared from; it does not re-read the on-chain
    owner.
    """
    owner_uid = resolve_owner_vote_uid(
        network=recipient.network,
        chain_identity=chain_identity,
        netuid=netuid,
        hotkeys=hotkeys,
        owner_hotkey=recipient.hotkey,
    )
    if owner_uid != recipient.uid:
        raise EmissionBlocked(
            "owner_snapshot_inconsistent",
            "Owner UID moved between selection and submission",
        )
    if (
        min_allowed_weights != 1
        or max_weight_limit is None
        or not max_weight_limit.is_finite()
        or max_weight_limit != Decimal(1)
    ):
        raise EmissionBlocked(
            "owner_vote_vector_invalid",
            "Owner vote requires chain minimum 1 and maximum 1",
        )
    if (
        len(uint_uids) != 1
        or len(uint_weights) != 1
        or uint_uids[0] != recipient.uid
        or uint_weights[0] != U16_MAX
    ):
        raise EmissionBlocked(
            "owner_vote_vector_invalid",
            "Owner vote requires the exact owner u16 vector",
        )
