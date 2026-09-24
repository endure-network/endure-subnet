"""Digest-covered chain classification and policy applicability."""

from __future__ import annotations

import pytest

from endure.protocol.consensus_policy import (
    MAINNET_GENESIS_HASH,
    TESTNET_GENESIS_HASH,
    ChainClass,
    chain_needs_genesis,
    chain_owner_vote_network,
    classify_chain,
    mainnet_policy_applies,
)

FINNEY = "wss://entrypoint-finney.opentensor.ai:443"
TESTNET = "wss://test.finney.opentensor.ai:443"
LOOPBACK = "ws://127.0.0.1:9944"
PRIVATE = "wss://node.operator.example:443"


@pytest.mark.parametrize(
    ("mock", "endpoint", "network", "genesis", "expected"),
    [
        (False, FINNEY, "finney", None, "mainnet"),
        (False, TESTNET, "test", None, "testnet"),
        (False, LOOPBACK, "local", None, "dev"),
        (False, LOOPBACK, "local", "0xlocalnet", "dev"),
        (False, LOOPBACK, "local", MAINNET_GENESIS_HASH, "mainnet"),
        (False, LOOPBACK, "local", TESTNET_GENESIS_HASH, "testnet"),
        (False, PRIVATE, PRIVATE, MAINNET_GENESIS_HASH, "mainnet"),
        (False, PRIVATE, PRIVATE, "0xother", "unrecognized"),
        (False, PRIVATE, PRIVATE, None, "unrecognized"),
        (True, FINNEY, "finney", MAINNET_GENESIS_HASH, "dev"),
    ],
)
def test_genesis_outranks_endpoint_names(
    mock: bool, endpoint: str, network: str, genesis: str | None, expected: str
) -> None:
    assert (
        classify_chain(mock=mock, endpoint=endpoint, network=network, genesis=genesis)
        == expected
    )


@pytest.mark.parametrize(
    ("mock", "endpoint", "network", "expected"),
    [
        (False, FINNEY, "finney", False),
        (False, TESTNET, "test", False),
        (False, LOOPBACK, "local", True),
        (False, PRIVATE, PRIVATE, True),
        (True, LOOPBACK, "local", False),
    ],
)
def test_only_live_non_aliased_endpoints_read_genesis(
    mock: bool, endpoint: str, network: str, expected: bool
) -> None:
    assert (
        chain_needs_genesis(mock=mock, endpoint=endpoint, network=network) is expected
    )


@pytest.mark.parametrize(
    ("chain", "served", "policy", "vote"),
    [
        ("mainnet", True, True, "mainnet"),
        ("testnet", True, False, "testnet"),
        ("dev", True, False, None),
        ("unrecognized", True, False, None),
        ("mainnet", False, False, None),
        ("testnet", False, False, None),
    ],
)
def test_served_mainnet_and_testnet_alone_get_policy_and_owner_vote(
    chain: ChainClass, served: bool, policy: bool, vote: str | None
) -> None:
    assert mainnet_policy_applies(chain, served=served) is policy
    assert chain_owner_vote_network(chain, served=served) == vote
