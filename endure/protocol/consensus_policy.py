"""Release-pinned Alpha Risk admission, eligibility, and owner-vote identity.

These values are protocol-digest inputs, not deployment tuning controls.
Local and testnet runtimes may override the admission settings for
development; mainnet validators must reject conflicting configuration before
opening transport.
"""

from decimal import Decimal
from typing import Final

MIN_MINER_STAKE: Final = Decimal("0")
MAX_COMMITS_PER_ROUND: Final = 10
MAX_REVEALS_PER_ROUND: Final = 10
EPOCH_LENGTH_BLOCKS: Final = 100
# Consecutive metagraph resync generations a scored hotkey must be absent
# before its EMA state is archived (fairness-deltas spec §1 decision 3).
DEREGISTRATION_CONFIRMATION_SYNCS: Final = 2

# Finney identity published by the Polkadot.js production-network registry:
# https://github.com/polkadot-js/common/blob/master/packages/networks/src/defaults/genesis.ts
MAINNET_GENESIS_HASH: Final = (
    "0x2f0555cc76fc2840a25a6ea3b9637146806f1f44b090c175ffde2a7e5ab36c03"
)
# Mainnet owner-vote pin: the fallback recipient is always the on-chain
# SubnetOwnerHotkey, and on mainnet it must also equal this approved hotkey.
SN30_NETUID: Final = 30
SN30_OWNER_HOTKEY: Final = "5HW12NvEZoGz8ZzcWMh4xyDUy6H1Af85m5LB8V1L11erK1S1"


def require_canonical_mainnet_policy(
    *,
    min_miner_stake: Decimal,
    max_commits_per_round: int,
    max_reveals_per_round: int,
    epoch_length: int,
) -> None:
    """Reject effective overrides instead of silently changing admission."""
    for option, actual, canonical in (
        ("endure.min_miner_stake", min_miner_stake, MIN_MINER_STAKE),
        ("endure.max_commits_per_round", max_commits_per_round, MAX_COMMITS_PER_ROUND),
        ("endure.max_reveals_per_round", max_reveals_per_round, MAX_REVEALS_PER_ROUND),
        ("neuron.epoch_length", epoch_length, EPOCH_LENGTH_BLOCKS),
    ):
        if actual != canonical:
            raise RuntimeError(
                f"--{option} must be {canonical} under the mainnet consensus policy"
            )
