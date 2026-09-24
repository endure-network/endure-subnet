"""Release-pinned Alpha Risk admission, eligibility, chain classification,
and owner-vote identity.

These values are protocol-digest inputs, not deployment tuning controls.
Local and testnet runtimes may override the admission settings for
development; mainnet validators must reject conflicting configuration before
opening transport.
"""

from decimal import Decimal
from typing import Final, Literal
from urllib.parse import urlparse

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
# Bittensor testnet (test.finney) genesis, read from its public entrypoint.
TESTNET_GENESIS_HASH: Final = (
    "0x8f9cf856bf558a14440e75569c9e58594757048d7b3a84b5d25f6bd978263105"
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


# Chain classification decides who runs mainnet policy and who emits the owner
# vote, so it is a digest input. The SDK plumbing that resolves the effective
# endpoint and reads the genesis hash stays in endure/utils/config.py.
ChainClass = Literal["mainnet", "testnet", "dev", "unrecognized"]
OwnerVoteNetwork = Literal["mainnet", "testnet"]

LOCAL_CHAIN_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1"})
_LOCAL_CHAIN_ENDPOINTS: Final = frozenset({"mock", "local", "localhost", "127.0.0.1"})
# Named testnet hosts. Keyed RPC providers ride --subtensor.network as a
# wss:// URL because bittensor >=10.3 silently drops
# --subtensor.chain_endpoint during network resolution. Extend deliberately:
# a wrong entry here opens a serving gate.
TESTNET_HOSTS: Final = frozenset(
    {"test.finney.opentensor.ai", "api-bittensor-testnet.n.dwellir.com"}
)
# Named mainnet hosts. Serving still requires the explicit
# --endure.serving_stage mainnet acknowledgement.
MAINNET_HOSTS: Final = frozenset(
    {
        "entrypoint-finney.opentensor.ai",
        "archive.chain.opentensor.ai",
        "lite.sub.latent.to",
        "api-bittensor-mainnet.n.dwellir.com",
    }
)
# bittensor's built-in --subtensor.network aliases that resolve to mainnet.
MAINNET_NETWORKS: Final = frozenset({"finney", "archive", "latent-lite"})


def endpoint_host(endpoint: str) -> str:
    endpoint = endpoint.strip()
    if not endpoint:
        return ""
    parsed = urlparse(endpoint if "://" in endpoint else f"//{endpoint}")
    return parsed.hostname or endpoint.split(":", maxsplit=1)[0]


def _named_class(*, endpoint: str, network: str) -> ChainClass | None:
    hosts = {endpoint_host(endpoint), endpoint_host(network)}
    if network in MAINNET_NETWORKS or hosts & MAINNET_HOSTS:
        return "mainnet"
    if network == "test" or hosts & TESTNET_HOSTS:
        return "testnet"
    return None


def chain_needs_genesis(*, mock: bool, endpoint: str, network: str) -> bool:
    """Only a live runtime on a non-aliased endpoint is classified by genesis."""
    return not mock and _named_class(endpoint=endpoint, network=network) is None


def classify_chain(
    *, mock: bool, endpoint: str, network: str, genesis: str | None
) -> ChainClass:
    """Classify the configured chain; a recorded genesis outranks endpoint names.

    An operator's own Finney node on loopback, a tunnel or a private host is
    mainnet by genesis, never a development chain.
    """
    if mock:
        return "dev"
    if genesis == MAINNET_GENESIS_HASH:
        return "mainnet"
    if genesis == TESTNET_GENESIS_HASH:
        return "testnet"
    local = (
        endpoint in _LOCAL_CHAIN_ENDPOINTS
        or endpoint_host(endpoint) in LOCAL_CHAIN_HOSTS
    )
    if local:
        return "dev"
    if genesis is not None:
        return "unrecognized"
    return _named_class(endpoint=endpoint, network=network) or "unrecognized"


def mainnet_policy_applies(chain: ChainClass, *, served: bool) -> bool:
    """Served Alpha Risk on mainnet runs the pinned mainnet consensus policy."""
    return served and chain == "mainnet"


def chain_owner_vote_network(
    chain: ChainClass, *, served: bool
) -> OwnerVoteNetwork | None:
    """Served Alpha Risk on mainnet or testnet votes for the owner when idle.

    Development and unrecognized chains keep abstaining.
    """
    if not served:
        return None
    if chain == "mainnet":
        return "mainnet"
    if chain == "testnet":
        return "testnet"
    return None
