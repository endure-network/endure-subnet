from __future__ import annotations

import pytest

from endure.assessment.lending_universe import (
    FORGE_LENDING_WHITELISTED_NETUIDS,
    LendingUniverseError,
    StaticLendingUniverseProvider,
    canonical_lending_universe_members,
    parse_lending_universe_members,
    validate_lending_netuid_membership,
)
from endure.assessment.registry import default_registry
from endure.assessment.schemas.forge_lending import FORGE_LENDING_SCHEMA_ID


def test_static_lending_provider_returns_canonical_whitelist_snapshot() -> None:
    provider = StaticLendingUniverseProvider()

    snapshot = provider.fetch_universe("2026-06-09")

    assert snapshot.round_id == "2026-06-09"
    assert snapshot.tickers == tuple(
        str(netuid) for netuid in sorted(FORGE_LENDING_WHITELISTED_NETUIDS)
    )
    assert len(snapshot.source_hash) == 64


def test_lending_provider_rejects_duplicate_netuids() -> None:
    provider = StaticLendingUniverseProvider(netuids=(44, 44))

    with pytest.raises(LendingUniverseError, match="unique"):
        provider.fetch_universe("2026-06-09")


def test_lending_provider_rejects_universe_over_cap() -> None:
    provider = StaticLendingUniverseProvider(netuids=(8, 44), max_targets=1)

    with pytest.raises(LendingUniverseError, match="targets > cap"):
        provider.fetch_universe("2026-06-09")


def test_lending_membership_accepts_only_whitelisted_netuid_values() -> None:
    universe = canonical_lending_universe_members((44, 8))

    assert validate_lending_netuid_membership(netuids=(8, 44), universe=universe)
    assert not validate_lending_netuid_membership(netuids=(288,), universe=universe)


def test_lending_universe_members_reject_non_canonical_forms() -> None:
    # Leading zeros and non-ASCII digits parse to the same int but do not match
    # the canonical ASCII producer, so they must fail closed rather than alias.
    for member in ("044", "٤٤", "4４"):
        with pytest.raises(LendingUniverseError, match="invalid lending netuid"):
            parse_lending_universe_members((member,))


def test_lending_universe_members_fail_closed_on_non_decimal_values() -> None:
    with pytest.raises(LendingUniverseError, match="invalid lending netuid"):
        parse_lending_universe_members(("WAL",))

    assert not validate_lending_netuid_membership(netuids=(44,), universe=("WAL",))


def test_registry_entry_exposes_static_lending_universe_provider() -> None:
    entry = default_registry().get(FORGE_LENDING_SCHEMA_ID)

    assert isinstance(entry.universe_provider, StaticLendingUniverseProvider)
