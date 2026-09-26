"""Miner admission for commit/reveal submissions.

Who may submit is consensus-relevant: absence-aware scoring gives a
previously scored miner zero observations at any validator that turns it
away, so every validator must admit by the same rule. The validator's axon
blacklist delegates here with its current metagraph view.
"""

from collections.abc import Callable, Sequence
from decimal import Decimal


def miner_admission(
    hotkey: str | None,
    *,
    registered_hotkeys: Sequence[str],
    stake_weight: Callable[[int], Decimal],
    min_stake: Decimal,
) -> tuple[bool, str]:
    """Return the axon blacklist verdict ``(blacklisted, reason)``.

    ``stake_weight`` maps a UID to its metagraph total stake weight (S), which
    combines alpha stake with discounted root TAO stake and is not a TAO
    balance. It is consulted only when ``min_stake`` is positive; mainnet pins
    the floor to zero.
    """
    if hotkey is None:
        return True, "Missing dendrite or hotkey"
    if hotkey not in registered_hotkeys:
        return True, "Unrecognized hotkey"
    if min_stake > 0:
        uid = registered_hotkeys.index(hotkey)
        if stake_weight(uid) < min_stake:
            return True, "Insufficient stake"
    return False, "Hotkey recognized"
