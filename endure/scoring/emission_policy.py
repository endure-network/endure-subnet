"""Pure, release-pinned emission planning: earned weights, burn, or owner vote.

When no miner holds a positive score, a validator on an owner-vote network
assigns its whole vote to the on-chain subnet owner instead of abstaining.
This is a standing fallback, not a one-time bootstrap: it applies at cold
start and again whenever every scored miner has been archived. The owner
allocation never enters scores or EMAs.

Once a miner scores, the owner still receives the burn rate its hotkey
publishes as its subnet commitment (``endure.burn_bps=<0..10000>``); miners
share the rest by earned weight. A missing or malformed commitment burns the
whole vote, so miners are paid only on the owner's explicit instruction. No
hotkey of the owner's coldkey earns weight at any rate: Subtensor withholds
their incentive, so the published rate is the whole burn.

Every mode plans from one chain snapshot: validator identity, permit and the
chain's strict weights rate limit decide whether an attempt is due, so a
rate-limited attempt is deferred instead of being refused by the SDK and
recorded as a failed submission.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Final, Literal, NamedTuple

from endure.protocol.consensus_policy import (
    MAINNET_GENESIS_HASH,
    SN30_NETUID,
    SN30_OWNER_HOTKEY,
    OwnerVoteNetwork,
    normalize_genesis_hash,
)
from endure.scoring.context import TR_CONTEXT
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
    "score_state_unavailable",
    "owner_commitment_unavailable",
]

# Owner burn rate: basis points of each earned vote that go to the owner UID.
BURN_BPS_DENOMINATOR: Final = 10_000
FULL_BURN_BPS: Final = BURN_BPS_DENOMINATOR
BURN_COMMITMENT_PREFIX: Final = "endure.burn_bps="
_BURN_COMMITMENT: Final = re.compile(r"endure\.burn_bps=(0|[1-9][0-9]{0,4})")
_RAW_COMMITMENT_FIELD: Final = re.compile(r"Raw([0-9]{1,3})")


class EmissionBlocked(ValueError):
    """The chain state cannot safely support this emission attempt."""

    def __init__(self, reason: EmissionBlockReason, detail: str) -> None:
        super().__init__(detail)
        self.reason: EmissionBlockReason = reason


class OwnerVoteRecipient(NamedTuple):
    """Owner context of one attempt, rechecked before the vector is sent.

    ``burn_bps`` is the owner's share of the vector: the whole vote for the
    owner vote, the published rate when earned weights share it, and ``0`` for
    a purely earned vote. ``withheld`` holds the UIDs the chain withholds
    (the owner and the owner coldkey's hotkeys), which never earn.
    """

    network: OwnerVoteNetwork
    hotkey: str
    uid: int
    burn_bps: int = FULL_BURN_BPS
    owner_coldkey: str | None = None
    withheld: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class ChainSnapshot:
    """The chain facts one emission attempt is planned from."""

    block: int
    hotkeys: Sequence[str]
    owner_hotkey: str | None
    owner_coldkey: str | None
    coldkeys: Sequence[str]
    validator_permit: Sequence[bool]
    last_update: Sequence[int]
    weights_rate_limit: int


# SDK ``SelectiveMetagraphIndex`` values that populate exactly the
# ``ChainSnapshot`` fields: Netuid 0 (always decoded), OwnerHotkey 5,
# OwnerColdkey 6, Block 7, WeightsRateLimit 27, Hotkeys 52, Coldkeys 53,
# ValidatorPermit 57, LastUpdate 59. The narrowed ``get_metagraph_info`` skips
# the stake/axon/identity vectors.
CHAIN_SNAPSHOT_METAGRAPH_INDICES: Final[tuple[int, ...]] = (
    0,
    5,
    6,
    7,
    27,
    52,
    53,
    57,
    59,
)


@dataclass(frozen=True, slots=True)
class OwnerCommitment:
    """One read of the owner's subnet commitment: whose, at which block, what."""

    hotkey: str
    block: int
    record: object


@dataclass(frozen=True, slots=True)
class OwnerState:
    """Owner facts re-read at the submission block, before a vote is sent."""

    block: int
    owner_hotkey: str | None
    owner_coldkey: str | None
    hotkeys: Sequence[str]
    coldkeys: Sequence[str]


# Netuid 0, OwnerHotkey 5, OwnerColdkey 6, Block 7, Hotkeys 52, Coldkeys 53.
OWNER_STATE_METAGRAPH_INDICES: Final[tuple[int, ...]] = (0, 5, 6, 7, 52, 53)


@dataclass(frozen=True, slots=True)
class EmissionPlan:
    due: bool
    permit: bool
    next_eligible_block: int
    weights: tuple[Decimal, ...]
    recipient: OwnerVoteRecipient | None
    # Owner share of this attempt; ``None`` where no owner-vote network applies.
    burn_bps: int | None = None
    # A due scored attempt still needs this owner hotkey's commitment: read it
    # at the snapshot block and plan again. Such a plan has no vector to send.
    commitment_owner: str | None = None


def select_emission_mode(
    scores: Sequence[Decimal], *, owner_vote_network: OwnerVoteNetwork | None
) -> EmissionMode:
    """Earned weights whenever any score is positive; otherwise the fallback."""
    if any(score > 0 for score in scores):
        return "scored"
    return "abstain" if owner_vote_network is None else "owner_vote"


def chain_withheld_uids(snapshot: ChainSnapshot, owner_uid: int) -> frozenset[int]:
    """UIDs whose miner incentive Subtensor withholds instead of paying.

    The chain burns (or recycles) the incentive of the subnet owner hotkey and
    of every registered hotkey of the owner's coldkey, so none of them earns.
    """
    if not snapshot.owner_coldkey or len(snapshot.coldkeys) != len(snapshot.hotkeys):
        raise EmissionBlocked(
            "owner_snapshot_inconsistent",
            "Snapshot lacks the owner coldkey or a coldkey per UID",
        )
    owned = (
        uid
        for uid, coldkey in enumerate(snapshot.coldkeys)
        if coldkey == snapshot.owner_coldkey
    )
    return frozenset((*owned, owner_uid))


def observed_burn_rate(
    commitment: OwnerCommitment | None, snapshot: ChainSnapshot
) -> int:
    """The burn rate one owner commitment read sets for this snapshot.

    No read burns the whole vote. A read of another hotkey or block, or a
    record newer than its read block, does not describe this snapshot.
    """
    if commitment is None:
        return FULL_BURN_BPS
    if commitment.hotkey != snapshot.owner_hotkey or commitment.block != snapshot.block:
        raise EmissionBlocked(
            "chain_snapshot_inconsistent",
            "Owner commitment was not read from the snapshot owner at its block",
        )
    record = commitment.record
    recorded = record.get("block") if isinstance(record, Mapping) else None
    if isinstance(recorded, int) and not isinstance(recorded, bool):
        if recorded > commitment.block:
            raise EmissionBlocked(
                "chain_snapshot_inconsistent",
                "Owner commitment is newer than the block it was read at",
            )
    return parse_burn_rate(commitment_text(record))


def burn_commitment_text(burn_bps: int) -> str:
    """The exact commitment an owner publishes for ``burn_bps``."""
    if isinstance(burn_bps, bool) or not 0 <= burn_bps <= FULL_BURN_BPS:
        raise ValueError(f"burn rate must be 0..{FULL_BURN_BPS} basis points")
    return f"{BURN_COMMITMENT_PREFIX}{burn_bps}"


def commitment_text(metadata: object) -> str | None:
    """Decode one on-chain commitment record to its text.

    ``None`` means no commitment exists (the SDK reports that as ``""``). A
    record that is not exactly one UTF-8 ``Raw`` field decodes to ``""``,
    which the burn rule treats as malformed.
    """
    if metadata is None or metadata == "":
        return None
    info = metadata.get("info") if isinstance(metadata, Mapping) else None
    fields = info.get("fields") if isinstance(info, Mapping) else None
    if not isinstance(fields, (list, tuple)) or len(fields) != 1:
        return ""
    field = fields[0]
    if not isinstance(field, Mapping) or len(field) != 1:
        return ""
    [(variant, value)] = field.items()
    match = (
        _RAW_COMMITMENT_FIELD.fullmatch(variant) if isinstance(variant, str) else None
    )
    if match is None or not isinstance(value, str):
        return ""
    try:
        data = bytes.fromhex(value.removeprefix("0x"))
        text = data.decode("utf-8")
    except ValueError:
        return ""
    return text if len(data) == int(match.group(1)) else ""


def parse_burn_rate(text: str | None) -> int:
    """Burn rate in basis points; anything but an exact commitment burns all."""
    match = None if text is None else _BURN_COMMITMENT.fullmatch(text)
    if match is None:
        return FULL_BURN_BPS
    burn_bps = int(match.group(1))
    return burn_bps if burn_bps <= FULL_BURN_BPS else FULL_BURN_BPS


def owner_burn_weights(
    scores: Sequence[Decimal], *, owner_uid: int, burn_bps: int
) -> tuple[Decimal, ...] | None:
    """Raw vector giving the owner ``burn_bps`` and miners the earned rest.

    Earned shares are the normalized positive scores of every UID but the
    owner's; callers zero the other chain-withheld UIDs first. ``None`` means
    no other UID holds a positive score, so the owner vote applies instead.
    """
    if not 0 <= owner_uid < len(scores):
        raise EmissionBlocked(
            "owner_snapshot_inconsistent", "Owner UID is outside the score vector"
        )
    with localcontext(TR_CONTEXT):
        earned = [
            score if uid != owner_uid and score > 0 else Decimal(0)
            for uid, score in enumerate(scores)
        ]
        total = sum(earned, Decimal(0))
        if total <= 0:
            return None
        burn = Decimal(burn_bps) / Decimal(BURN_BPS_DENOMINATOR)
        miners = Decimal(1) - burn
        weights = [miners * score / total for score in earned]
        weights[owner_uid] = burn
        return tuple(weights)


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
    owner_commitment: OwnerCommitment | None = None,
) -> EmissionPlan:
    """Plan one attempt from one snapshot, or raise ``EmissionBlocked``.

    ``owner_commitment`` is the owner hotkey's commitment read at the snapshot
    block; it sets the burn rate of a scored attempt. Without it, a due scored
    attempt on an owner-vote network returns ``commitment_owner`` and no
    vector, so the commitment is read only once owner, permit and rate limit
    allow the attempt.
    """
    if snapshot is None or snapshot.block != block:
        raise EmissionBlocked(
            "chain_snapshot_inconsistent", "No chain snapshot at this block"
        )
    recipient: OwnerVoteRecipient | None = None
    weights = tuple(scores)
    burn_bps: int | None = None
    needs_commitment = False
    if mode == "scored":
        # Earned weight follows a hotkey, never a UID: a slot re-registered on
        # chain but not yet in the local metagraph must not receive it.
        for uid, score in enumerate(scores):
            if score > 0 and (
                uid >= len(local_hotkeys)
                or uid >= len(snapshot.hotkeys)
                or local_hotkeys[uid] != snapshot.hotkeys[uid]
            ):
                raise EmissionBlocked(
                    "chain_snapshot_inconsistent",
                    "A scored UID's hotkey differs between the local metagraph "
                    "and the chain snapshot",
                )
    if mode == "owner_vote" and network is None:
        raise EmissionBlocked(
            "owner_vote_chain_mismatch", "Owner vote requires a vote network"
        )
    if network is not None:
        owner_uid = _local_owner_uid(
            network=network,
            chain_identity=chain_identity,
            netuid=netuid,
            snapshot=snapshot,
            local_hotkeys=local_hotkeys,
        )
        if not snapshot.owner_coldkey:
            raise EmissionBlocked(
                "owner_snapshot_inconsistent", "Snapshot has no subnet owner coldkey"
            )
        withheld = (
            chain_withheld_uids(snapshot, owner_uid)
            if mode == "scored"
            else frozenset((owner_uid,))
        )
        earned = tuple(
            Decimal(0) if uid in withheld else score for uid, score in enumerate(scores)
        )
        scored = mode == "scored" and any(score > 0 for score in earned)
        needs_commitment = scored and owner_commitment is None
        weights, burn_bps = _vote_network_weights(
            earned=earned,
            owner_uid=owner_uid,
            size=len(local_hotkeys),
            burn_bps=(
                observed_burn_rate(owner_commitment, snapshot)
                if scored and not needs_commitment
                else FULL_BURN_BPS
            ),
        )
        recipient = OwnerVoteRecipient(
            network,
            local_hotkeys[owner_uid],
            owner_uid,
            burn_bps,
            snapshot.owner_coldkey,
            withheld,
        )
    due = submission_due(
        validator_uid=validator_uid,
        validator_hotkey=validator_hotkey,
        hotkeys=snapshot.hotkeys,
        validator_permits=snapshot.validator_permit,
        last_updates=snapshot.last_update,
        block=block,
        weights_rate_limit=snapshot.weights_rate_limit,
    )
    next_eligible_block = (
        snapshot.last_update[validator_uid] + snapshot.weights_rate_limit + 1
    )
    if needs_commitment:
        # The rate is unknown until the commitment is read: nothing to send.
        return EmissionPlan(
            due=due,
            permit=bool(snapshot.validator_permit[validator_uid]),
            next_eligible_block=next_eligible_block,
            weights=(),
            recipient=None,
            burn_bps=None,
            commitment_owner=snapshot.owner_hotkey if due else None,
        )
    return EmissionPlan(
        due=due,
        permit=bool(snapshot.validator_permit[validator_uid]),
        next_eligible_block=next_eligible_block,
        weights=weights,
        recipient=recipient,
        burn_bps=burn_bps,
    )


def _vote_network_weights(
    *,
    earned: tuple[Decimal, ...],
    owner_uid: int,
    size: int,
    burn_bps: int,
) -> tuple[tuple[Decimal, ...], int]:
    """Raw vector and owner share on an owner-vote network.

    ``earned`` already holds no chain-withheld UID, so weight on it would never
    be burned on top of the published rate.
    """
    if burn_bps == 0 and any(score > 0 for score in earned):
        return earned, 0
    if 0 < burn_bps < FULL_BURN_BPS:
        burned = owner_burn_weights(earned, owner_uid=owner_uid, burn_bps=burn_bps)
        if burned is not None:
            return burned, burn_bps
    return owner_vote_weights(size, owner_uid), FULL_BURN_BPS


def _local_owner_uid(
    *,
    network: OwnerVoteNetwork,
    chain_identity: str,
    netuid: int,
    snapshot: ChainSnapshot,
    local_hotkeys: Sequence[str],
) -> int:
    """Resolve the chain owner and require the local metagraph to agree."""
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
    return owner_uid


def resolve_owner_vote_uid(
    *,
    network: OwnerVoteNetwork,
    chain_identity: str,
    netuid: int,
    hotkeys: Sequence[str],
    owner_hotkey: str | None,
) -> int:
    """Resolve the on-chain subnet owner to its UID in one metagraph snapshot."""
    chain_identity = normalize_genesis_hash(chain_identity) if chain_identity else ""
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
    if recipient.burn_bps != FULL_BURN_BPS:
        _recheck_owner_burn_share(
            recipient, hotkeys=hotkeys, uint_uids=uint_uids, uint_weights=uint_weights
        )
        # The owner earns only its burn; no other withheld UID earns at all.
        earning = (
            recipient.withheld
            if recipient.burn_bps == 0
            else recipient.withheld - {recipient.uid}
        )
        if any(uid in earning for uid in uint_uids):
            raise EmissionBlocked(
                "owner_vote_vector_invalid",
                "A chain-withheld UID would receive earned weight",
            )
        return
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


def _recheck_owner_burn_share(
    recipient: OwnerVoteRecipient,
    *,
    hotkeys: Sequence[str],
    uint_uids: Sequence[int],
    uint_weights: Sequence[int],
) -> None:
    """Require the owner's u16 share to equal the burn rate up to rounding.

    The encoder scales the largest entry to the u16 maximum and rounds each
    exact value by at most one half. With ``n`` UIDs and u16 total ``T >= 65535``
    the owner's share can therefore differ from the burn rate by at most
    ``(n + 1) / (2T)``, under 0.2% for 256 UIDs; the check is exact integer
    arithmetic and also enforces its premises.
    """
    if (
        not uint_weights
        or len(uint_uids) != len(uint_weights)
        or len(set(uint_uids)) != len(uint_uids)
        or (recipient.burn_bps > 0 and recipient.uid not in uint_uids)
        or any(not 0 <= uid < len(hotkeys) for uid in uint_uids)
        or any(weight <= 0 or weight > U16_MAX for weight in uint_weights)
        or max(uint_weights) != U16_MAX
    ):
        raise EmissionBlocked(
            "owner_vote_vector_invalid",
            "Owner burn requires a max-scaled u16 vector with one owner entry",
        )
    if recipient.burn_bps == 0:
        return
    owner_u16 = uint_weights[uint_uids.index(recipient.uid)]
    total = sum(uint_weights)
    deviation = abs(BURN_BPS_DENOMINATOR * owner_u16 - recipient.burn_bps * total)
    if 2 * deviation > BURN_BPS_DENOMINATOR * (len(hotkeys) + 1):
        raise EmissionBlocked(
            "owner_vote_vector_invalid",
            "Owner share of the u16 vector does not match the burn rate",
        )


def recheck_owner_state(
    recipient: OwnerVoteRecipient,
    state: OwnerState | None,
    *,
    block: int | None,
    prepared_hotkeys: Sequence[str],
    uint_uids: Sequence[int],
) -> None:
    """Require the owner facts a prepared vector relies on to hold at sending.

    ``state`` is re-read at the submission block. The subnet owner hotkey and
    coldkey must be unchanged, and every UID the vector names, plus the owner
    UID, must keep its hotkey and its withheld status, so a burned share never
    reaches a former owner and earned weight never reaches a withheld UID.
    """
    if state is None or block is None or state.block != block:
        raise EmissionBlocked(
            "chain_snapshot_inconsistent", "No owner state at the submission block"
        )
    if (
        state.owner_hotkey != recipient.hotkey
        or state.owner_coldkey != recipient.owner_coldkey
    ):
        raise EmissionBlocked(
            "owner_snapshot_inconsistent", "Subnet owner changed before submission"
        )
    if len(state.coldkeys) != len(state.hotkeys):
        raise EmissionBlocked(
            "owner_snapshot_inconsistent", "Owner state lacks a coldkey per UID"
        )
    for uid in {*uint_uids, recipient.uid}:
        if (
            not 0 <= uid < len(state.hotkeys)
            or uid >= len(prepared_hotkeys)
            or state.hotkeys[uid] != prepared_hotkeys[uid]
        ):
            raise EmissionBlocked(
                "chain_snapshot_inconsistent",
                "A UID in the vector changed hands before submission",
            )
        withheld = uid == recipient.uid or state.coldkeys[uid] == state.owner_coldkey
        if withheld != (uid in recipient.withheld):
            raise EmissionBlocked(
                "owner_snapshot_inconsistent",
                "A UID in the vector changed withheld status before submission",
            )
