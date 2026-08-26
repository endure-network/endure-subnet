"""Static Alpha Risk V1 target universe (risk scope spec §Universe).

The launch whitelist is versioned in watched Python, snapshotted per round, and
grown only by PR. R2 selection criterion is deep liquidity plus recorded data
availability; dynamic selection is deferred by risk scope §Explicitly deferred.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from endure.assessment.schemas.subnet_alpha_risk import SubnetAlphaTarget
from endure.assessment.universe import UniverseSnapshot

# risk scope §Universe: curated launch list for reliable pool depth/data. Netuids
# 8 and 44 stay included because current recorded fixtures cover them.
ALPHA_RISK_WHITELISTED_NETUIDS = (1, 3, 4, 5, 8, 9, 11, 13, 19, 44, 51, 64)
MAX_ALPHA_RISK_TARGETS_PER_ROUND = len(ALPHA_RISK_WHITELISTED_NETUIDS)


class AlphaRiskUniverseError(ValueError):
    """Raised when an Alpha Risk target universe is malformed or out of policy."""


def canonical_alpha_risk_universe_members(netuids: tuple[int, ...]) -> tuple[str, ...]:
    """Return sorted, unique netuid members encoded for UniverseSnapshot."""
    targets = tuple(SubnetAlphaTarget(netuid=netuid).netuid for netuid in netuids)
    if len(set(targets)) != len(targets):
        raise AlphaRiskUniverseError("alpha risk netuid whitelist must be unique")
    return tuple(str(netuid) for netuid in sorted(targets))


def alpha_risk_universe_source_hash(members: tuple[str, ...]) -> str:
    """Digest the exact whitelist members stored in the round snapshot."""
    return hashlib.sha256("\n".join(members).encode("utf-8")).hexdigest()


def parse_alpha_risk_universe_members(members: tuple[str, ...]) -> frozenset[int]:
    """Parse a stored Alpha Risk universe into netuids, failing closed."""
    parsed: list[int] = []
    for member in members:
        if not (member.isascii() and member.isdecimal() and str(int(member)) == member):
            raise AlphaRiskUniverseError(
                f"invalid alpha risk netuid member: {member!r}"
            )
        parsed.append(SubnetAlphaTarget(netuid=int(member)).netuid)
    if len(set(parsed)) != len(parsed):
        raise AlphaRiskUniverseError("alpha risk universe contains duplicate netuids")
    return frozenset(parsed)


def validate_alpha_risk_netuid_membership(
    *, netuids: tuple[int, ...], universe: tuple[str, ...]
) -> bool:
    """True when every submitted Alpha Risk netuid is in the frozen universe."""
    try:
        members = parse_alpha_risk_universe_members(universe)
    except AlphaRiskUniverseError:
        return False
    return all(netuid in members for netuid in netuids)


@dataclass(frozen=True, slots=True)
class StaticAlphaRiskUniverseProvider:
    """Static whitelist provider for Alpha Risk V1 launch rounds."""

    netuids: tuple[int, ...] = ALPHA_RISK_WHITELISTED_NETUIDS
    max_targets: int = MAX_ALPHA_RISK_TARGETS_PER_ROUND

    def fetch_universe(self, round_id: str) -> UniverseSnapshot:
        members = canonical_alpha_risk_universe_members(self.netuids)
        if len(members) > self.max_targets:
            raise AlphaRiskUniverseError(
                f"alpha risk universe has {len(members)} targets > cap {self.max_targets}"
            )
        return UniverseSnapshot(
            round_id=round_id,
            tickers=members,
            source_hash=alpha_risk_universe_source_hash(members),
        )
