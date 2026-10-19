"""Payout pooling of exact-duplicate forecasts (Sybil containment).

Hotkeys that reveal identical forecast values are pooled. Over the newest
``POOL_WINDOW_ROUNDS`` revealed rounds, each hotkey is assigned to the oldest
identical group it joined, and the hotkeys assigned to one group form a pool.
A pool weighs what its best member would alone: every member gives up the
same amount of sharpened score, or all of its own if that is less (see
``normalize_weights``). Equal copies split one hotkey's weight, and hotkeys
that join a pool never cost its other members more, in total, than they give
up themselves. Pools never overlap, so a copy that matches different hotkeys
in different rounds shares only one pool. Only the values are compared, never
nonces or free text, and only emission weights pool; scores, EMAs and
consensus weighting are unchanged.

Only a registered hotkey with a positive blended score counts toward a pool,
so an unscored or deregistered copy never costs another hotkey anything.

This is a containment, not a Sybil defense: grouping is exact, so forecasts
that differ in any value or in coverage never pool. While the scored hotkeys
stay the same, a hotkey stays in the oldest group it joined until that round
leaves the window, so leaving or re-forming a group does not pay before then.
Membership is live: a hotkey that is first scored, registers again or
deregisters can move others between pools, and one that starts matching a pool
later joins it at the latest once its first matching round is the window's
oldest. An original cannot be told from its copy, so a predictable forecast
pools with a hotkey that reproduces it. The weight both give up is
renormalized across every miner, the copier's other hotkeys included, which
pays only an operator already earning more than half of the miners' pool.

The vote rounds each pool's total once and shares its units out by
remainder. Members whose remainders tie take units in an order drawn from the
window's seed (``tie_ordered``). Live validators seed it with the hash of the
first finalized block stamped at least ``TIE_SEED_DELAY`` after the newest
round's reveal close (``chain_tie_seed``). No one knows that hash while hotkeys
are registered or the round's forecasts revealed, so no hotkey can be picked in
advance to lead the order. Once it is known, a scored hotkey that has
deregistered can still be registered again, or swapped in, to rejoin its pool,
at the price of a registration or a swap. Every validator with the same window
draws the same order, so consensus does not clip the units they split.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Iterable, Mapping
from datetime import timedelta
from typing import Final

from endure.assessment.coordinates import AssessmentCoordinate
from endure.scoring.policy import DEFAULT_PAYOUT_HALF_LIFE_ROUNDS

POOL_WINDOW_ROUNDS: Final = DEFAULT_PAYOUT_HALF_LIFE_ROUNDS
# A round seeds its window's tie order with the first finalized block stamped
# at least this long after its reveal close, so even a validator whose clock
# runs this far behind has stopped accepting the round's reveals before anyone
# knows it.
TIE_SEED_DELAY: Final = timedelta(minutes=1)
# Keeps the tie order's hash apart from any other use of the same seed.
TIE_ORDER_DOMAIN: Final = "endure.pool_tie_order.v1"

type IdenticalGroups = tuple[frozenset[str], ...]
# Each pool's hotkeys in the order that breaks its equal remainders.
type OrderedPools = tuple[tuple[str, ...], ...]


def identical_forecast_groups(
    accepted: Mapping[str, Mapping[AssessmentCoordinate, int]],
) -> IdenticalGroups:
    """One round's hotkeys that submitted identical values, two or more a group.

    ``accepted`` maps the round's accepted hotkeys to their submitted values.
    An empty submission forecasts nothing and joins no group.
    """
    groups: dict[frozenset[tuple[AssessmentCoordinate, int]], set[str]] = {}
    for hotkey, values in accepted.items():
        if values:
            groups.setdefault(frozenset(values.items()), set()).add(hotkey)
    return tuple(frozenset(members) for members in groups.values() if len(members) > 1)


def duplicate_pools(
    rounds: Iterable[IdenticalGroups], scored: Collection[str] | None = None
) -> tuple[frozenset[str], ...]:
    """The pools of two or more hotkeys across ``rounds``, given newest first.

    Each hotkey is assigned to the oldest group it joined, and its pool is
    the hotkeys assigned to the same group. With ``scored``, only those
    hotkeys count as members, so a group pools only while two or more of its
    copies are still registered and scored.
    """
    assigned: dict[str, tuple[int, int]] = {}
    for age, groups in reversed(list(enumerate(rounds))):
        for index, group in enumerate(groups):
            members = group if scored is None else group.intersection(scored)
            if len(members) > 1:
                for hotkey in members:
                    assigned.setdefault(hotkey, (age, index))
    pools: dict[tuple[int, int], set[str]] = {}
    for hotkey, group in assigned.items():
        pools.setdefault(group, set()).add(hotkey)
    pooled = (frozenset(members) for members in pools.values() if len(members) > 1)
    return tuple(sorted(pooled, key=sorted))


def tie_ordered(pools: Iterable[Collection[str]], seed: str) -> OrderedPools:
    """Each pool's hotkeys in the order drawn from ``seed``.

    The order is a hash of ``seed`` and each hotkey, so every validator with
    the same seed draws the same one. Validators that gave a tied unit to
    different members would each be clipped by consensus, which can cost a
    pool of sub-unit copies its whole payout. A seed known before hotkeys are
    registered would let an operator grind one that leads the order, so live
    validators draw it from the chain after the window's newest reveal closes.
    """
    return tuple(
        tuple(
            sorted(
                pool,
                key=lambda hotkey: hashlib.sha256(
                    f"{TIE_ORDER_DOMAIN}:{seed}:{hotkey}".encode()
                ).digest(),
            )
        )
        for pool in pools
    )


def pool_sizes_of(pools: Iterable[Collection[str]]) -> dict[str, int]:
    """Each pooled hotkey's pool size."""
    return {hotkey: len(pool) for pool in pools for hotkey in pool}
