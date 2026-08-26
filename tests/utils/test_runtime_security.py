import argparse
from unittest.mock import patch

import bittensor as bt
import pytest
from bittensor.core.subtensor import Subtensor

from endure.runtime.live import LiveRuntimeProvider
from endure.runtime.mock import MockRuntimeProvider
from endure.runtime.resolve import resolve_runtime_provider
from endure.utils.config import (
    DevOnlyConfigError,
    add_args,
    permits_dev_only_runtime,
    require_compression_runtime_allowed,
    require_serving_stage_allowed,
)


class _Args:
    pass


def _parsed_config(*args: str) -> bt.Config:
    parser = argparse.ArgumentParser()
    bt.Subtensor.add_args(parser)
    add_args(_Args, parser)
    parser.add_argument("--runtime.mode", default="live")
    return bt.Config(parser, args=list(args))


def test_explicit_live_runtime_ignores_legacy_mock_flag(
    production_miner_config: bt.Config,
) -> None:
    vars(production_miner_config)["mock"] = True
    production_miner_config.subtensor.chain_endpoint = ""
    production_miner_config.subtensor.network = "test"

    assert permits_dev_only_runtime(production_miner_config) is False


@pytest.mark.parametrize(
    ("mode", "legacy_mock", "provider_type"),
    (
        ("mock", False, MockRuntimeProvider),
        ("live", True, LiveRuntimeProvider),
        (None, True, MockRuntimeProvider),
        (None, False, LiveRuntimeProvider),
    ),
)
def test_runtime_provider_resolution(
    mode: str | None,
    legacy_mock: bool,
    provider_type: type[LiveRuntimeProvider] | type[MockRuntimeProvider],
) -> None:
    config = bt.Config()
    config.mock = legacy_mock
    if mode is not None:
        config.runtime = argparse.Namespace(mode=mode)

    assert isinstance(resolve_runtime_provider(config), provider_type)


def test_effective_remote_endpoint_overrides_raw_local_value(
    production_miner_config: bt.Config,
) -> None:
    production_miner_config.subtensor.chain_endpoint = "ws://127.0.0.1:9944"
    production_miner_config.subtensor.network = "test"

    with patch.object(
        bt.Subtensor,
        "setup_config",
        return_value=("wss://test.finney.opentensor.ai:443", "test"),
    ):
        assert permits_dev_only_runtime(production_miner_config) is False


def test_raw_local_endpoint_that_resolves_to_finney_is_live() -> None:
    config = _parsed_config("--subtensor.chain_endpoint", "ws://127.0.0.1:9944")

    endpoint, network = Subtensor.setup_config(None, config)

    assert network == "finney"
    assert endpoint == "wss://entrypoint-finney.opentensor.ai:443"
    assert permits_dev_only_runtime(config) is False


def test_effective_finney_rejects_raw_testnet_serving_and_compression() -> None:
    config = _parsed_config(
        "--subtensor.chain_endpoint",
        "test",
        "--endure.serving_stage",
        "testnet",
    )

    with pytest.raises(DevOnlyConfigError, match="mainnet serving"):
        require_serving_stage_allowed(config)
    with pytest.raises(DevOnlyConfigError, match="mainnet compression"):
        require_compression_runtime_allowed(config)


def test_effective_local_and_test_networks_keep_supported_dev_paths() -> None:
    local = _parsed_config("--subtensor.network", "ws://127.0.0.1:9944")
    testnet = _parsed_config(
        "--subtensor.network", "test", "--endure.serving_stage", "testnet"
    )

    assert permits_dev_only_runtime(local) is True
    require_serving_stage_allowed(testnet)
    require_compression_runtime_allowed(testnet)
