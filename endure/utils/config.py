# The MIT License (MIT)
# Copyright © 2023 Yuma Rao
# Copyright © 2023 Opentensor Foundation

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

import argparse
import os
from collections.abc import Callable
from decimal import Decimal, InvalidOperation

import bittensor as bt
from bittensor.core.subtensor import Subtensor

from endure.assessment.registry import (
    SchemaRegistry,
    SchemaRegistryEntry,
    SchemaServingStatus,
    UnknownSchemaError,
    default_registry,
)
from endure.assessment.schemas.subnet_alpha_risk import RISK_SCHEMA_ID
from endure.protocol.consensus_policy import (
    EPOCH_LENGTH_BLOCKS,
    MAX_COMMITS_PER_ROUND,
    MAX_REVEALS_PER_ROUND,
    MIN_MINER_STAKE,
    PROTOCOL_CONSENSUS_SETTINGS,
    ChainClass,
    OwnerVoteNetwork,
    PinnedConsensusSettings,
    chain_needs_genesis,
    chain_owner_vote_network,
    classify_chain,
    consensus_settings_pinned,
    ignored_consensus_settings,
    mainnet_policy_applies,
    normalize_genesis_hash,
    require_emitting_validator_serves_axon,
    serves_alpha_risk,
)

from .logging import safe_endpoint_label

# bittensor >=10.3 disabled bt.Config CLI/arg parsing by default
# (BT_NO_PARSE_CLI_ARGS defaults to "true"), so bt.Config(parser, args=...)
# returns only built-in defaults and drops every custom --netuid/--neuron.*
# argument. Our neuron entrypoints and tests build config from argparse and
# depend on that parsing, so opt back in unless an operator overrides it.
os.environ.setdefault("BT_NO_PARSE_CLI_ARGS", "false")


class DevOnlyConfigError(RuntimeError):
    """Raised when dev-only runtime settings are pointed at non-local chain."""


def _chain_endpoint(config: "bt.Config") -> str:
    subtensor = getattr(config, "subtensor", None)
    endpoint = getattr(subtensor, "chain_endpoint", None)
    if endpoint:
        return str(endpoint)
    network = getattr(subtensor, "network", "")
    return str(network or "")


def _effective_chain(config: "bt.Config") -> tuple[str, str]:
    endpoint, network = Subtensor.setup_config(None, config)
    return str(endpoint or "").strip(), str(network or "").strip()


def _is_mock_runtime(config: "bt.Config") -> bool:
    runtime = getattr(config, "runtime", None)
    runtime_mode = str(getattr(runtime, "mode", ""))
    if runtime_mode == "mock":
        return True
    return runtime_mode != "live" and bool(getattr(config, "mock", False))


def _resolved_genesis(config: "bt.Config") -> str | None:
    section = getattr(config, "endure", None)
    genesis = getattr(section, "chain_genesis_hash", None)
    return genesis if isinstance(genesis, str) else None


def resolve_chain_identity(
    config: "bt.Config", *, read_genesis: Callable[[str], str | None]
) -> None:
    """Record the connected chain's genesis before any policy gate reads it.

    Endpoint names cannot identify an operator's own Finney node reached over
    loopback, an SSH tunnel, ``--subtensor.network local`` or a private host;
    the watched ``classify_chain`` uses the recorded genesis instead.
    """
    endpoint, network = _effective_chain(config)
    # Always overwrite: a value carried in from an operator config file must
    # never classify a chain this process did not read.
    config.endure.chain_genesis_hash = None
    if not chain_needs_genesis(
        mock=_is_mock_runtime(config), endpoint=endpoint, network=network
    ):
        return
    genesis = read_genesis(endpoint)
    if genesis is None:
        raise RuntimeError(
            f"cannot identify the chain at {safe_endpoint_label(endpoint)}"
        )
    config.endure.chain_genesis_hash = normalize_genesis_hash(genesis)


def chain_class(config: "bt.Config") -> ChainClass:
    endpoint, network = _effective_chain(config)
    return classify_chain(
        mock=_is_mock_runtime(config),
        endpoint=endpoint,
        network=network,
        genesis=_resolved_genesis(config),
    )


def permits_dev_only_runtime(config: "bt.Config") -> bool:
    """True only for mock runtimes or local chains that are not Finney/testnet.

    Risk scope §Dev-only time compression.
    """
    return chain_class(config) == "dev"


def uses_mainnet_consensus_policy(config: "bt.Config") -> bool:
    """Select live Alpha Risk mainnet policy for the classified chain."""
    return mainnet_policy_applies(
        chain_class(config), served=requires_serving_stage_gate(config)
    )


def owner_vote_network(config: "bt.Config") -> OwnerVoteNetwork | None:
    return chain_owner_vote_network(
        chain_class(config), served=requires_serving_stage_gate(config)
    )


def apply_consensus_settings(config: "bt.Config") -> None:
    """Normalize the effective config to the protocol consensus settings.

    On served mainnet and testnet an operator value is ignored with a warning
    instead of refused: v0.1.0 advised a positive stake floor on live networks,
    and a refusal would crash-loop those operators on the next image pull.
    """
    chain = chain_class(config)
    if not consensus_settings_pinned(chain, served=requires_serving_stage_gate(config)):
        return
    given = PinnedConsensusSettings(
        min_miner_stake=Decimal(str(config.endure.min_miner_stake)),
        max_commits_per_round=int(config.endure.max_commits_per_round),
        max_reveals_per_round=int(config.endure.max_reveals_per_round),
        epoch_length=int(config.neuron.epoch_length),
    )
    for ignored in ignored_consensus_settings(given):
        bt.logging.warning(
            f"--{ignored.option} {ignored.given} is ignored on {chain}; "
            f"running the protocol value {ignored.protocol}"
        )
    protocol = PROTOCOL_CONSENSUS_SETTINGS
    config.endure.min_miner_stake = protocol.min_miner_stake
    config.endure.max_commits_per_round = protocol.max_commits_per_round
    config.endure.max_reveals_per_round = protocol.max_reveals_per_round
    config.neuron.epoch_length = protocol.epoch_length


def require_mainnet_validator_policy(config: "bt.Config") -> None:
    """Fail before transport startup on a mainnet option that cannot be ignored."""
    if not uses_mainnet_consensus_policy(config):
        return
    require_emitting_validator_serves_axon(
        axon_off=bool(config.neuron.axon_off),
        disable_set_weights=bool(config.neuron.disable_set_weights),
    )


def requires_serving_stage_gate(
    config: "bt.Config", registry: SchemaRegistry | None = None
) -> bool:
    entry = active_schema_entry(config, registry)
    return serves_alpha_risk(
        schema_id=entry.schema.schema_id, serving_status=entry.serving_status
    )


def require_serving_stage_allowed(
    config: "bt.Config", registry: SchemaRegistry | None = None
) -> None:
    if not requires_serving_stage_gate(config, registry):
        return
    if permits_dev_only_runtime(config):
        return

    section = getattr(config, "endure", None)
    serving_stage = None if section is None else getattr(section, "serving_stage", None)
    endpoint = safe_endpoint_label(_effective_chain(config)[0])
    if chain_class(config) == "testnet":
        if serving_stage == "testnet":
            return
        raise DevOnlyConfigError(
            "risk.v1.subnet_alpha serving on Bittensor testnet requires "
            "--endure.serving_stage testnet; configured endpoint "
            f"{endpoint!r} is refused"
        )

    if chain_class(config) == "mainnet":
        if serving_stage == "mainnet":
            return
        raise DevOnlyConfigError(
            "risk.v1.subnet_alpha mainnet serving requires the explicit "
            "--endure.serving_stage mainnet acknowledgement; configured "
            f"endpoint {endpoint!r} is refused"
        )

    raise DevOnlyConfigError(
        "risk.v1.subnet_alpha serving requires a recognized Bittensor testnet "
        f"or mainnet endpoint; configured endpoint {endpoint!r} is refused"
    )


def require_dev_only_runtime(config: "bt.Config", *, feature: str) -> None:
    """Refuse dev-only R5 knobs unless the chain endpoint is mock/local."""
    if permits_dev_only_runtime(config):
        return
    endpoint = safe_endpoint_label(_effective_chain(config)[0])
    raise DevOnlyConfigError(
        f"{feature} is dev-only and requires mock or ws://127.0.0.1 subtensor; "
        f"configured endpoint {endpoint!r} is not allowed "
        "(risk scope §Dev-only time compression)"
    )


def require_explicit_netuid(config: "bt.Config") -> None:
    """Refuse the argparse ``--netuid`` default outside mock/local chains.

    The default keeps mock runs and tests terse, but on a live network a
    defaulted netuid silently registers, serves and weighs against whatever
    subnet carries that id, so the operator must name it. bt.Config records
    which parameters the command line supplied, so the default itself can
    stay untouched.
    """
    if permits_dev_only_runtime(config) or config.is_set("netuid"):
        return
    endpoint = safe_endpoint_label(_effective_chain(config)[0])
    raise DevOnlyConfigError(
        "--netuid was not provided; pass --netuid explicitly on live networks "
        "(the default is accepted only for mock or local chains; configured "
        f"endpoint {endpoint!r})"
    )


def require_compression_runtime_allowed(config: "bt.Config") -> None:
    if permits_dev_only_runtime(config):
        return

    section = getattr(config, "endure", None)
    serving_stage = None if section is None else getattr(section, "serving_stage", None)
    endpoint = safe_endpoint_label(_effective_chain(config)[0])
    if chain_class(config) == "testnet":
        if serving_stage == "testnet":
            return
        raise DevOnlyConfigError(
            "--endure.devnet_time_compression on Bittensor testnet requires "
            "the explicit --endure.serving_stage testnet acknowledgement; "
            f"configured endpoint {endpoint!r} is refused. Mainnet compression "
            "is always refused."
        )

    raise DevOnlyConfigError(
        "--endure.devnet_time_compression is allowed only on mock/local chains, "
        "or on Bittensor testnet with --endure.serving_stage testnet; mainnet "
        "compression is always refused. "
        f"Configured endpoint {endpoint!r} is refused."
    )


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {parsed}")
    return parsed


def _non_negative_decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"invalid Decimal value: {value!r}") from exc
    if not parsed.is_finite() or parsed < Decimal("0"):
        raise argparse.ArgumentTypeError(
            f"must be a finite non-negative Decimal, got {parsed}"
        )
    return parsed


def _registered_schema_id(value: str) -> str:
    registry = default_registry()
    try:
        registry.get(value)
    except UnknownSchemaError as exc:
        known = ", ".join(registry.schema_ids())
        raise argparse.ArgumentTypeError(
            f"unknown schema_id {value!r}; known schemas: {known}"
        ) from exc
    return value


def active_schema_entry(
    config: "bt.Config", registry: SchemaRegistry | None = None
) -> SchemaRegistryEntry:
    """Resolve ``--endure.active_schema`` against the registry."""
    section = getattr(config, "endure", None)
    schema_id = (
        RISK_SCHEMA_ID
        if section is None
        else str(getattr(section, "active_schema", RISK_SCHEMA_ID))
    )
    return (registry or default_registry()).get(schema_id)


def active_schema_id(
    config: "bt.Config", registry: SchemaRegistry | None = None
) -> str:
    return active_schema_entry(config, registry).schema.schema_id


def active_runtime_schema_entry(
    config: "bt.Config",
    registry: SchemaRegistry | None = None,
    *,
    admitted_statuses: tuple[SchemaServingStatus, ...] = ("served",),
) -> SchemaRegistryEntry:
    """Resolve the active schema and fail closed if its registry status is inactive."""
    entry = active_schema_entry(config, registry)
    if entry.serving_status in admitted_statuses:
        return entry

    section = getattr(config, "endure", None)
    allow_unserved = bool(
        False
        if section is None
        else getattr(section, "allow_unserved_schema_for_dev", False)
    )
    if allow_unserved and entry.serving_status == "registered_unserved":
        require_dev_only_runtime(
            config, feature="--endure.allow_unserved_schema_for_dev"
        )
        return entry

    statuses = ", ".join(admitted_statuses)
    raise RuntimeError(
        f"active schema {entry.schema.schema_id!r} is registered/selectable with "
        f"serving_status {entry.serving_status!r}, but this neuron runtime admits "
        f"only {statuses} schemas; registered-unserved schemas wait for activation "
        "gates"
    )


def active_runtime_schema_id(
    config: "bt.Config", registry: SchemaRegistry | None = None
) -> str:
    return active_runtime_schema_entry(config, registry).schema.schema_id


def check_config(cls, config: "bt.Config"):
    r"""Checks/validates the config namespace object."""
    bt.logging.check_config(config)

    full_path = os.path.expanduser(
        "{}/{}/{}/netuid{}/{}".format(
            config.logging.logging_dir,  # Align this path with the neuron directory naming convention.
            config.wallet.name,
            config.wallet.hotkey,
            config.netuid,
            config.neuron.name,
        )
    )
    config.neuron.full_path = os.path.expanduser(full_path)
    if not os.path.exists(config.neuron.full_path):
        os.makedirs(config.neuron.full_path, exist_ok=True)

    endure_section = getattr(config, "endure", None)
    if bool(
        False
        if endure_section is None
        else getattr(endure_section, "devnet_time_compression", False)
    ):
        require_compression_runtime_allowed(config)

    warn_ignored_options(config)


# Options that no longer do anything. Each stays accepted so an existing start
# script keeps working, is ignored with a warning, and is deleted at the next
# protocol key change.
_IGNORED_VALUE_OPTIONS = (
    "endure.fetch_delay_seconds",
    "neuron.events_retention_size",
    "neuron.moving_average_alpha",
)
_IGNORED_FLAG_OPTIONS = ("neuron.dont_save_events",)
_IGNORED_OPTION_HELP = "Ignored; removed at the next protocol key change."


def _add_ignored_options(parser, options: tuple[str, ...]) -> None:
    for option in options:
        if option in _IGNORED_FLAG_OPTIONS:
            parser.add_argument(
                f"--{option}",
                action="store_true",
                default=None,
                help=_IGNORED_OPTION_HELP,
            )
        else:
            parser.add_argument(
                f"--{option}", type=str, default=None, help=_IGNORED_OPTION_HELP
            )


def warn_ignored_options(config: "bt.Config") -> None:
    """Warn for every supplied option that no longer has any effect."""
    for option in (*_IGNORED_VALUE_OPTIONS, *_IGNORED_FLAG_OPTIONS):
        section, name = option.split(".", maxsplit=1)
        value = getattr(getattr(config, section, None), name, None)
        if value is None:
            continue
        given = "" if option in _IGNORED_FLAG_OPTIONS else f" {value}"
        bt.logging.warning(
            f"--{option}{given} is ignored and will be removed at the next "
            "protocol key change; delete it from start scripts"
        )


def add_args(cls, parser):
    """
    Adds relevant arguments to the parser for operation.
    """

    parser.add_argument(
        "--netuid",
        type=int,
        help=(
            "Subnet netuid. Default 1 targets mainnet/testnet; on localnet the "
            "assigned netuid is usually higher (often 2 — netuid 0 is reserved "
            "for root). Check `btcli subnets list --network <endpoint>` and pass "
            "the actual netuid explicitly when running against local chains."
        ),
        default=1,
    )

    parser.add_argument(
        "--neuron.epoch_length",
        type=_positive_int,
        help="Metagraph refresh and weight-attempt interval in blocks; served testnet/mainnet ignore it and run the protocol value.",
        default=EPOCH_LENGTH_BLOCKS,
    )

    _add_ignored_options(
        parser,
        (
            "neuron.events_retention_size",
            "neuron.dont_save_events",
            "endure.fetch_delay_seconds",
        ),
    )

    parser.add_argument(
        "--endure.active_schema",
        type=_registered_schema_id,
        default=RISK_SCHEMA_ID,
        help=(
            "Schema selected for the neuron runtime. The default is the served "
            "Alpha Risk V1 schema; lending.v1.subnet_asset remains "
            "registered_unserved/dormant."
        ),
    )
    parser.add_argument(
        "--endure.market_data_endpoint",
        type=str,
        default="wss://archive.chain.opentensor.ai:443",
        help=(
            "Dedicated mainnet archive endpoint for Alpha Risk market data. "
            "This is independent of --subtensor.* (the subnet chain endpoint)."
        ),
    )
    parser.add_argument(
        "--endure.allow_unserved_schema_for_dev",
        action="store_true",
        default=False,
        help=(
            "Dev-only override admitting registered_unserved schemas on mock/local "
            "chains for pre-serving milestones; refused on testnet/mainnet."
        ),
    )
    parser.add_argument(
        "--endure.serving_stage",
        choices=("testnet", "mainnet"),
        default=None,
        help=(
            "Explicit serving-stage acknowledgement for Alpha Risk. The named "
            "stage must match the configured chain endpoint; serving on a live "
            "network is refused without it."
        ),
    )
    parser.add_argument(
        "--endure.devnet_time_compression",
        action="store_true",
        default=False,
        help=(
            "Alpha Risk compressed round windows and horizon due times. Allowed "
            "on mock/local chains, or on Bittensor testnet only with "
            "--endure.serving_stage testnet; always refused on mainnet."
        ),
    )
    parser.add_argument(
        "--endure.devnet_round_seconds",
        type=_positive_int,
        default=60,
        help="Wall seconds per compressed Alpha Risk devnet round.",
    )
    parser.add_argument(
        "--endure.devnet_horizon_5d_seconds",
        type=_positive_int,
        default=5,
        help="Compressed due time for the Alpha Risk 5d pass in devnet runs.",
    )
    parser.add_argument(
        "--endure.devnet_horizon_30d_seconds",
        type=_positive_int,
        default=10,
        help="Compressed due time for the Alpha Risk 30d pass in devnet runs.",
    )
    parser.add_argument(
        "--endure.database_url",
        type=str,
        default="sqlite:///var/endure.db",
        help="SQLAlchemy URL for the validator's round/score database.",
    )
    parser.add_argument(
        "--endure.synthetic_epoch",
        type=str,
        default="",
        help=(
            "ISO timestamp anchoring the synthetic scheduler; all neurons in a "
            "compressed run must share it."
        ),
    )
    parser.add_argument(
        "--endure.tick_seconds",
        type=_positive_int,
        default=12,
        help="Seconds between round-service ticks.",
    )
    parser.add_argument(
        "--endure.health_tick_max_age_seconds",
        type=_positive_int,
        default=300,
        help=(
            "Maximum age of the validator's last tick attempt before "
            "health/watchdog marks the process stale."
        ),
    )
    parser.add_argument(
        "--endure.health_startup_grace_seconds",
        type=_positive_int,
        default=300,
        help=(
            "Startup grace period before a validator with no tick attempt is "
            "marked stale."
        ),
    )
    parser.add_argument(
        "--endure.resolution_budget_seconds",
        type=_positive_int,
        default=600,
        help=(
            "Wall-clock budget for target resolution within a single tick. "
            "Work exceeding it is deferred to the next tick via the "
            "partially_scored resumption path, keeping every tick well under "
            "health_tick_max_duration_seconds (a 30d horizon needs thousands "
            "of paced archive RPCs — hours of work no single tick may carry)."
        ),
    )
    parser.add_argument(
        "--endure.health_tick_max_duration_seconds",
        type=_positive_int,
        default=1800,
        help=(
            "Maximum wall-clock duration of a single in-flight tick or sync "
            "operation before the watchdog marks the process stale. Long "
            "catch-up ticks (multi-round resolution backlogs) legitimately "
            "exceed health_tick_max_age_seconds, so while an operation is in "
            "flight the watchdog applies this longer window anchored at the "
            "operation start; a wedged operation that exceeds it still trips "
            "the watchdog."
        ),
    )
    parser.add_argument(
        "--endure.min_miner_stake",
        type=_non_negative_decimal,
        default=MIN_MINER_STAKE,
        help=(
            "Minimum miner metagraph stake weight S to accept commits/reveals "
            "(not a TAO balance). Served testnet/mainnet ignore it and run the "
            "protocol zero floor; only mock/local chains honor it."
        ),
    )
    parser.add_argument(
        "--endure.max_commits_per_round",
        type=_positive_int,
        default=MAX_COMMITS_PER_ROUND,
        help="Per-miner commit rate limit per round; served testnet/mainnet run the protocol value.",
    )
    parser.add_argument(
        "--endure.max_reveals_per_round",
        type=_positive_int,
        default=MAX_REVEALS_PER_ROUND,
        help="Per-miner reveal rate limit per round; served testnet/mainnet run the protocol value.",
    )
    parser.add_argument(
        "--endure.api_port",
        type=int,
        default=0,
        help="Validator read-API port; 0 disables the API.",
    )
    parser.add_argument(
        "--endure.api_host",
        type=str,
        default="127.0.0.1",
        help="Validator read-API bind host.",
    )


def add_miner_args(cls, parser):
    """Add miner specific arguments to the parser."""

    parser.add_argument(
        "--neuron.name",
        type=str,
        help="Trials for this neuron go in neuron.root / (wallet_cold - wallet_hot) / neuron.name. ",
        default="miner",
    )

    parser.add_argument(
        "--blacklist.force_validator_permit",
        action="store_true",
        help="If set, we will force incoming requests to have a permit.",
        default=False,
    )

    parser.add_argument(
        "--blacklist.allow_non_registered",
        action="store_true",
        help="If set, miners will accept queries from non registered entities. (Dangerous!)",
        default=False,
    )

    parser.add_argument(
        "--runtime.mode",
        type=str,
        choices=("live", "mock"),
        default="live",
        help="Runtime provider mode for the miner entrypoint.",
    )
    parser.add_argument(
        "--mock",
        action="store_const",
        const="mock",
        dest="runtime.mode",
        help="Compatibility alias for --runtime.mode mock.",
    )

    parser.add_argument(
        "--endure.validator_axon_overrides",
        type=str,
        default="",
        help=(
            "Comma-separated hotkey=host:port overrides for validator axons "
            "when a colocated miner must bypass the on-chain public address. "
            "Endpoints are trusted internal plaintext peers; bracket IPv6 hosts."
        ),
    )

    parser.add_argument(
        "--endure.min_validator_stake_weight",
        type=_non_negative_decimal,
        default=Decimal("0"),
        help=(
            "Minimum metagraph total stake weight (S) for a permit-holding peer "
            "to receive pushes; 0 disables the gate. S combines alpha stake "
            "with discounted root TAO stake and is not a TAO balance."
        ),
    )


def add_validator_args(cls, parser):
    """Add validator specific arguments to the parser."""

    parser.add_argument(
        "--neuron.name",
        type=str,
        help="Trials for this neuron go in neuron.root / (wallet_cold - wallet_hot) / neuron.name. ",
        default="validator",
    )

    _add_ignored_options(parser, ("neuron.moving_average_alpha",))

    parser.add_argument(
        "--neuron.num_concurrent_forwards",
        type=_positive_int,
        help="The number of concurrent forwards running at any time.",
        default=1,
    )

    parser.add_argument(
        "--neuron.disable_set_weights",
        action="store_true",
        help="Disables setting weights.",
        default=False,
    )

    parser.add_argument(
        "--neuron.axon_off",
        "--axon_off",
        action="store_true",
        # Note: the validator needs to serve an Axon with their IP or they may
        #   be blacklisted by the firewall of serving peers on the network.
        help="Set this flag to not attempt to serve an Axon.",
        default=False,
    )

    parser.add_argument(
        "--runtime.mode",
        type=str,
        choices=("live", "mock"),
        default="live",
        help="Runtime provider mode for the validator entrypoint.",
    )
    parser.add_argument(
        "--mock",
        action="store_const",
        const="mock",
        dest="runtime.mode",
        help="Compatibility alias for --runtime.mode mock.",
    )


def config(cls):
    """
    Returns the configuration object specific to this miner or validator after adding relevant arguments.
    """
    parser = argparse.ArgumentParser()
    bt.Wallet.add_args(parser)
    bt.Subtensor.add_args(parser)
    bt.logging.add_args(parser)
    bt.Axon.add_args(parser)
    cls.add_args(parser)
    return bt.Config(parser)
