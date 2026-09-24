"""Pure, release-pinned owner-vote fallback selection and safety checks.

When no miner holds a positive score, a validator on an owner-vote network
assigns its whole vote to the on-chain subnet owner instead of abstaining.
This is a standing fallback, not a one-time bootstrap: it applies at cold
start and again whenever every scored miner has been archived. The owner
allocation never enters scores or EMAs.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Literal, NamedTuple

from endure.protocol.consensus_policy import (
    MAINNET_GENESIS_HASH,
    SN30_NETUID,
    SN30_OWNER_HOTKEY,
)
from endure.scoring.weight_processing import U16_MAX

EmissionMode = Literal["scored", "owner_vote", "abstain"]
OwnerVoteNetwork = Literal["mainnet", "testnet"]
OwnerVoteBlockReason = Literal[
    "owner_vote_chain_mismatch",
    "owner_hotkey_mismatch",
    "owner_unregistered",
    "owner_snapshot_inconsistent",
    "validator_identity_invalid",
    "owner_vote_vector_invalid",
]


class OwnerVoteBlocked(ValueError):
    """The owner vote cannot safely use the supplied chain state."""

    def __init__(self, reason: OwnerVoteBlockReason, detail: str) -> None:
        super().__init__(detail)
        self.reason: OwnerVoteBlockReason = reason


class OwnerVoteRecipient(NamedTuple):
    """Owner resolved for one attempt, rechecked against its prepared vector."""

    network: OwnerVoteNetwork
    hotkey: str
    uid: int


def select_emission_mode(
    scores: Sequence[Decimal], *, owner_vote_network: OwnerVoteNetwork | None
) -> EmissionMode:
    """Earned weights whenever any score is positive; otherwise the fallback."""
    if any(score > 0 for score in scores):
        return "scored"
    return "abstain" if owner_vote_network is None else "owner_vote"


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
            raise OwnerVoteBlocked(
                "owner_vote_chain_mismatch", "Mainnet owner vote is pinned to SN30"
            )
        if owner_hotkey != SN30_OWNER_HOTKEY:
            raise OwnerVoteBlocked(
                "owner_hotkey_mismatch", "Subnet owner is not the pinned SN30 owner"
            )
    if not owner_hotkey:
        raise OwnerVoteBlocked(
            "owner_snapshot_inconsistent", "Snapshot has no subnet owner hotkey"
        )
    matches = [uid for uid, hotkey in enumerate(hotkeys) if hotkey == owner_hotkey]
    if not matches:
        raise OwnerVoteBlocked("owner_unregistered", "Subnet owner is not registered")
    if len(matches) != 1:
        raise OwnerVoteBlocked(
            "owner_snapshot_inconsistent", "Subnet owner appears at multiple UIDs"
        )
    return matches[0]


def owner_vote_weights(size: int, owner_uid: int) -> tuple[Decimal, ...]:
    """One-hot raw vector over the local metagraph used for the attempt."""
    if not 0 <= owner_uid < size:
        raise OwnerVoteBlocked(
            "owner_snapshot_inconsistent", "Owner UID is outside the local metagraph"
        )
    return tuple(Decimal(1) if uid == owner_uid else Decimal(0) for uid in range(size))


def owner_vote_submission_due(  # noqa: PLR0913 — explicit chain snapshot and identity
    *,
    validator_uid: int,
    validator_hotkey: str,
    hotkeys: Sequence[str],
    validator_permits: Sequence[bool],
    last_updates: Sequence[int],
    block: int,
    weights_rate_limit: int,
) -> bool:
    """Check current validator identity, permit, and inclusive rate boundary."""
    if validator_uid < 0 or validator_uid >= len(hotkeys):
        raise OwnerVoteBlocked(
            "validator_identity_invalid", "Validator UID is not registered"
        )
    if not validator_hotkey or hotkeys[validator_uid] != validator_hotkey:
        raise OwnerVoteBlocked(
            "validator_identity_invalid", "Validator UID does not match its hotkey"
        )
    if validator_uid >= len(validator_permits) or validator_uid >= len(last_updates):
        raise OwnerVoteBlocked(
            "owner_snapshot_inconsistent", "Validator metadata is missing"
        )
    last_update = last_updates[validator_uid]
    if block < 0 or weights_rate_limit < 0 or last_update < 0 or last_update > block:
        raise OwnerVoteBlocked(
            "owner_snapshot_inconsistent", "Incoherent snapshot block or rate data"
        )
    return (
        validator_permits[validator_uid] and block - last_update >= weights_rate_limit
    )


def validate_owner_vote_vector(
    *,
    owner_uid: int,
    uids: Sequence[int],
    weights: Sequence[int],
    min_allowed_weights: int | None,
    max_weight_limit: Decimal | None,
) -> None:
    """Reject chain constraints or processing that alter the owner allocation."""
    if (
        min_allowed_weights != 1
        or max_weight_limit is None
        or not max_weight_limit.is_finite()
        or max_weight_limit != Decimal(1)
    ):
        raise OwnerVoteBlocked(
            "owner_vote_vector_invalid",
            "Owner vote requires chain minimum 1 and maximum 1",
        )
    if (
        len(uids) != 1
        or len(weights) != 1
        or uids[0] != owner_uid
        or weights[0] != U16_MAX
    ):
        raise OwnerVoteBlocked(
            "owner_vote_vector_invalid",
            "Owner vote requires the exact owner u16 vector",
        )
