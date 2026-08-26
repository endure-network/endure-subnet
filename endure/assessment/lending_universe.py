"""Static Forge lending target universe (Forge lending scope spec §V1).

The Batch 4 activation gate keeps the lending universe in watched Python so
netuid membership is covered by the protocol digest while lending remains
registered but unserved. Production serving must confirm the final whitelist
before the default flips.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from endure.assessment.schemas.forge_lending import SubnetAssetTarget
from endure.assessment.universe import UniverseSnapshot

# Forge scope candidates: mainnet 44/8 for realistic soak; 288/333/418 are a
# pure-testnet fallback. Final production serving must consciously confirm the
# set before lending becomes the default-served schema.
FORGE_LENDING_WHITELISTED_NETUIDS = (8, 44, 288, 333, 418)
# The default cap intentionally tracks the candidate whitelist length. Operators
# can lower it for tests or staged rollouts, but adding a target to the watched
# whitelist should update the default fan-out allowance in the same digest fold.
MAX_LENDING_TARGETS_PER_ROUND = len(FORGE_LENDING_WHITELISTED_NETUIDS)


class LendingUniverseError(ValueError):
    """Raised when a lending target universe is malformed or out of policy."""


def canonical_lending_universe_members(netuids: tuple[int, ...]) -> tuple[str, ...]:
    """Return sorted, unique netuid members encoded for UniverseSnapshot."""
    targets = tuple(SubnetAssetTarget(netuid=netuid).netuid for netuid in netuids)
    if len(set(targets)) != len(targets):
        raise LendingUniverseError("lending netuid whitelist must be unique")
    return tuple(str(netuid) for netuid in sorted(targets))


def lending_universe_source_hash(members: tuple[str, ...]) -> str:
    """Digest the exact whitelist members stored in the round snapshot."""
    return hashlib.sha256("\n".join(members).encode("utf-8")).hexdigest()


def parse_lending_universe_members(members: tuple[str, ...]) -> frozenset[int]:
    """Parse a stored lending universe into netuids, failing closed."""
    parsed: list[int] = []
    for member in members:
        # Only the canonical ASCII form is a member: reject non-ASCII digits
        # and leading zeros, which parse to the same int but differ from the
        # canonical producer's output and so break the round's source_hash.
        if not (member.isascii() and member.isdecimal() and str(int(member)) == member):
            raise LendingUniverseError(f"invalid lending netuid member: {member!r}")
        parsed.append(SubnetAssetTarget(netuid=int(member)).netuid)
    if len(set(parsed)) != len(parsed):
        raise LendingUniverseError("lending universe contains duplicate netuids")
    return frozenset(parsed)


def validate_lending_netuid_membership(
    *, netuids: tuple[int, ...], universe: tuple[str, ...]
) -> bool:
    """True when every submitted lending netuid is in the frozen universe."""
    try:
        members = parse_lending_universe_members(universe)
    except LendingUniverseError:
        return False
    return all(netuid in members for netuid in netuids)


@dataclass(frozen=True, slots=True)
class StaticLendingUniverseProvider:
    """Static whitelist provider for the lending walking skeleton."""

    netuids: tuple[int, ...] = FORGE_LENDING_WHITELISTED_NETUIDS
    max_targets: int = MAX_LENDING_TARGETS_PER_ROUND

    def fetch_universe(self, round_id: str) -> UniverseSnapshot:
        members = canonical_lending_universe_members(self.netuids)
        if len(members) > self.max_targets:
            raise LendingUniverseError(
                f"lending universe has {len(members)} targets > cap {self.max_targets}"
            )
        return UniverseSnapshot(
            round_id=round_id,
            tickers=members,
            source_hash=lending_universe_source_hash(members),
        )
