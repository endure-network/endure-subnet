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
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

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

from .logging import safe_endpoint_label, setup_events_logger

# bittensor >=10.3 disabled bt.Config CLI/arg parsing by default
# (BT_NO_PARSE_CLI_ARGS defaults to "true"), so bt.Config(parser, args=...)
# returns only built-in defaults and drops every custom --netuid/--neuron.*
# argument. Our neuron entrypoints and tests build config from argparse and
# depend on that parsing, so opt back in unless an operator overrides it.
os.environ.setdefault("BT_NO_PARSE_CLI_ARGS", "false")

_LOCAL_CHAIN_HOSTS = {"localhost", "127.0.0.1", "::1"}
# Hosts the serving-stage gate accepts as Bittensor TESTNET. Keyed RPC
# providers ride --subtensor.network as a wss:// URL because bittensor >=10.3
# silently drops --subtensor.chain_endpoint during network resolution
# (Subtensor.setup_config evaluates candidates without breaking, so the
# always-set network default wins). Extend deliberately: a wrong entry here
# opens the mainnet serving gate.
_TESTNET_HOSTS = {
    "test.finney.opentensor.ai",
    "api-bittensor-testnet.n.dwellir.com",
}


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


def permits_dev_only_runtime(config: "bt.Config") -> bool:
    """True only for mock or local chain endpoints (risk scope §Dev-only time compression)."""
    runtime = getattr(config, "runtime", None)
    runtime_mode = str(getattr(runtime, "mode", ""))
    if runtime_mode == "mock":
        return True
    if runtime_mode != "live" and bool(getattr(config, "mock", False)):
        return True
    endpoint, _network = _effective_chain(config)
    if endpoint in {"mock", "local", "localhost", "127.0.0.1"}:
        return True
    return _host_of(endpoint) in _LOCAL_CHAIN_HOSTS


def _host_of(endpoint: str) -> str:
    endpoint = endpoint.strip()
    if not endpoint:
        return ""
    parsed = urlparse(endpoint if "://" in endpoint else f"//{endpoint}")
    return parsed.hostname or endpoint.split(":", maxsplit=1)[0]


def _is_bittensor_testnet(config: "bt.Config") -> bool:
    endpoint, network = _effective_chain(config)
    if network == "test":
        return True
    return bool({_host_of(endpoint), _host_of(network)} & _TESTNET_HOSTS)


def requires_serving_stage_gate(
    config: "bt.Config", registry: SchemaRegistry | None = None
) -> bool:
    entry = active_schema_entry(config, registry)
    return entry.schema.schema_id == RISK_SCHEMA_ID and entry.serving_status == "served"


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
    if _is_bittensor_testnet(config):
        if serving_stage == "testnet":
            return
        raise DevOnlyConfigError(
            "risk.v1.subnet_alpha serving on Bittensor testnet requires "
            "--endure.serving_stage testnet; configured endpoint "
            f"{endpoint!r} is refused"
        )

    raise DevOnlyConfigError(
        "risk.v1.subnet_alpha serving is blocked until the R7 soak gate passes "
        "(AGENTS.md / risk scope §R7); mainnet serving requires a code change "
        f"after soak approval. Configured endpoint {endpoint!r} is refused"
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


def require_compression_runtime_allowed(config: "bt.Config") -> None:
    if permits_dev_only_runtime(config):
        return

    section = getattr(config, "endure", None)
    serving_stage = None if section is None else getattr(section, "serving_stage", None)
    endpoint = safe_endpoint_label(_effective_chain(config)[0])
    if _is_bittensor_testnet(config):
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


def _unit_interval_decimal(value: str) -> Decimal:
    # EMA alpha is a score-affecting weight (spec §8): parse as Decimal (not
    # float, per the Decimal policy) and reject anything outside [0, 1], since
    # alpha > 1 inverts the moving average and NaN/negative corrupts scores.
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"invalid Decimal value: {value!r}") from exc
    if parsed.is_nan() or not (Decimal("0") <= parsed <= Decimal("1")):
        raise argparse.ArgumentTypeError(f"must be a Decimal in [0, 1], got {parsed}")
    return parsed


def _non_negative_decimal(value: str) -> Decimal:
    # An economic threshold (TAO stake): parse as Decimal per the Decimal
    # policy and reject NaN/negatives at boot, so a bad --endure.min_miner_stake
    # fails at startup rather than on the first inbound synapse.
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"invalid Decimal value: {value!r}") from exc
    if parsed.is_nan() or parsed < Decimal("0"):
        raise argparse.ArgumentTypeError(
            f"must be a non-negative Decimal, got {parsed}"
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

    if not config.neuron.dont_save_events:
        # Add custom event logger for the events.
        events_logger = setup_events_logger(
            config.neuron.full_path, config.neuron.events_retention_size
        )
        bt.logging.register_primary_logger(events_logger.name)


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
        help="The default epoch length (how often we set weights, measured in 12 second blocks).",
        default=100,
    )

    parser.add_argument(
        "--neuron.events_retention_size",
        type=_positive_int,
        help="Events retention size in bytes (passed to RotatingFileHandler.maxBytes).",
        default=2 * 1024 * 1024 * 1024,  # 2 GB
    )

    parser.add_argument(
        "--neuron.dont_save_events",
        action="store_true",
        help="If set, we dont save events to a log file.",
        default=False,
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
        choices=("testnet",),
        default=None,
        help=(
            "Explicit serving-stage acknowledgement for Alpha Risk. Only "
            "'testnet' is accepted; mainnet serving requires a code change after "
            "the R7 soak gate passes."
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
        "--endure.fetch_delay_seconds",
        type=_positive_int,
        default=72000,
        help="Settled-data delay after a horizon's close before outcome fetch.",
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
        "--endure.min_miner_stake",
        type=_non_negative_decimal,
        default=Decimal("0"),
        help=(
            "Minimum miner stake (TAO) to accept commits/reveals; 0 disables "
            "the gate (validator-only). Parsed as Decimal — TAO is an "
            "economic value."
        ),
    )
    parser.add_argument(
        "--endure.max_commits_per_round",
        type=_positive_int,
        default=10,
        help="Per-miner commit rate limit per round.",
    )
    parser.add_argument(
        "--endure.max_reveals_per_round",
        type=_positive_int,
        default=10,
        help="Per-miner reveal rate limit per round.",
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


def add_validator_args(cls, parser):
    """Add validator specific arguments to the parser."""

    parser.add_argument(
        "--neuron.name",
        type=str,
        help="Trials for this neuron go in neuron.root / (wallet_cold - wallet_hot) / neuron.name. ",
        default="validator",
    )

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
        "--neuron.moving_average_alpha",
        type=_unit_interval_decimal,
        help="Moving average alpha parameter, how much to add of the new observation.",
        default=Decimal("0.1"),
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
