"""Pure, release-pinned emission planning: earned weights, burn, or owner vote.

When no miner holds a positive score, a validator on an owner-vote network
assigns its whole vote to the on-chain subnet owner instead of abstaining.
This is a standing fallback, not a one-time bootstrap: it applies at cold
start and again whenever every scored miner has been archived. The owner
allocation never enters scores or EMAs.

Once a miner scores, the owner still receives the burn rate its hotkey
publishes as its subnet commitment (``endure.burn_bps=<0..10000>``); miners
share the rest by earned weight. A missing or malformed commitment burns the
whole vote, so miners are paid only on the owner's explicit instruction.

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
    """Owner resolved for one attempt, rechecked against its prepared vector.

    ``burn_bps`` is the owner's share of that vector: the whole vote for the
    owner vote, the published burn rate when earned weights share it.
    """

    network: OwnerVoteNetwork
    hotkey: str
    uid: int
    burn_bps: int = FULL_BURN_BPS


@dataclass(frozen=True, slots=True)
class ChainSnapshot:
    """The chain facts one emission attempt is planned from."""

    block: int
    hotkeys: Sequence[str]
    owner_hotkey: str | None
    validator_permit: Sequence[bool]
    last_update: Sequence[int]
    weights_rate_limit: int


# SDK ``SelectiveMetagraphIndex`` values that populate exactly the
# ``ChainSnapshot`` fields: Netuid 0 (always decoded), OwnerHotkey 5, Block 7,
# WeightsRateLimit 27, Hotkeys 52, ValidatorPermit 57, LastUpdate 59. The
# narrowed ``get_metagraph_info`` skips the stake/axon/identity vectors.
CHAIN_SNAPSHOT_METAGRAPH_INDICES: Final[tuple[int, ...]] = (0, 5, 7, 27, 52, 57, 59)


@dataclass(frozen=True, slots=True)
class EmissionPlan:
    due: bool
    permit: bool
    next_eligible_block: int
    weights: tuple[Decimal, ...]
    recipient: OwnerVoteRecipient | None
    # Owner share of this attempt; ``None`` where no owner-vote network applies.
    burn_bps: int | None = None


def select_emission_mode(
    scores: Sequence[Decimal], *, owner_vote_network: OwnerVoteNetwork | None
) -> EmissionMode:
    """Earned weights whenever any score is positive; otherwise the fallback."""
    if any(score > 0 for score in scores):
        return "scored"
    return "abstain" if owner_vote_network is None else "owner_vote"


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
    owner's. ``None`` means no other UID holds a positive score, so the owner
    vote applies instead.
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
    owner_commitment: object = None,
) -> EmissionPlan:
    """Plan one attempt from one snapshot, or raise ``EmissionBlocked``.

    ``owner_commitment`` is the owner hotkey's raw commitment record read at
    the snapshot block; it sets the burn rate of a scored attempt.
    """
    if snapshot is None or snapshot.block != block:
        raise EmissionBlocked(
            "chain_snapshot_inconsistent", "No chain snapshot at this block"
        )
    recipient: OwnerVoteRecipient | None = None
    weights = tuple(scores)
    burn_bps: int | None = None
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
        burn_bps = (
            FULL_BURN_BPS
            if mode == "owner_vote"
            else parse_burn_rate(commitment_text(owner_commitment))
        )
        if 0 < burn_bps < FULL_BURN_BPS:
            burned = owner_burn_weights(scores, owner_uid=owner_uid, burn_bps=burn_bps)
            if burned is None:
                # No other UID holds a positive score: the owner's vote is whole.
                burn_bps = FULL_BURN_BPS
            else:
                weights = burned
        if burn_bps == FULL_BURN_BPS:
            weights = owner_vote_weights(len(local_hotkeys), owner_uid)
        if burn_bps > 0:
            recipient = OwnerVoteRecipient(
                network, local_hotkeys[owner_uid], owner_uid, burn_bps
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
    return EmissionPlan(
        due=due,
        permit=bool(snapshot.validator_permit[validator_uid]),
        next_eligible_block=(
            snapshot.last_update[validator_uid] + snapshot.weights_rate_limit + 1
        ),
        weights=weights,
        recipient=recipient,
        burn_bps=burn_bps,
    )


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

    Each u16 entry rounds its exact value by at most one half, so with ``n``
    UIDs and u16 total ``T`` the owner's share can differ from the burn rate
    by at most ``(n + 1) / (2T)``; the check is exact integer arithmetic.
    """
    if (
        len(uint_uids) != len(uint_weights)
        or uint_uids.count(recipient.uid) != 1
        or any(weight <= 0 or weight > U16_MAX for weight in uint_weights)
    ):
        raise EmissionBlocked(
            "owner_vote_vector_invalid",
            "Owner burn requires one owner entry in a positive u16 vector",
        )
    owner_u16 = uint_weights[uint_uids.index(recipient.uid)]
    total = sum(uint_weights)
    deviation = abs(BURN_BPS_DENOMINATOR * owner_u16 - recipient.burn_bps * total)
    if 2 * deviation > BURN_BPS_DENOMINATOR * (len(hotkeys) + 1):
        raise EmissionBlocked(
            "owner_vote_vector_invalid",
            "Owner share of the u16 vector does not match the burn rate",
        )
