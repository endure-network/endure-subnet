"""Tests for endure.utils.config.

check_config and config() need a realistic bittensor config namespace — we build one via
the supported bt.Wallet / bt.Subtensor / bt.logging add_args path and
inject a tmp_path into logging.logging_dir.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import bittensor as bt
import pytest

from endure.assessment.registry import SchemaRegistry, SchemaRegistryEntry
from endure.assessment.schemas.forge_lending import (
    FORGE_LENDING_SCHEMA_ID,
    LendingSubmissionBundle,
    build_lending_v1_subnet_asset_schema,
)
from endure.assessment.schemas.subnet_alpha_risk import RISK_SCHEMA_ID
from endure.protocol.consensus_policy import MAINNET_GENESIS_HASH, TESTNET_GENESIS_HASH
from endure.utils.config import (
    DevOnlyConfigError,
    active_runtime_schema_entry,
    active_runtime_schema_id,
    active_schema_id,
    add_args,
    add_miner_args,
    add_validator_args,
    apply_consensus_settings,
    check_config,
    config,
    owner_vote_network,
    permits_dev_only_runtime,
    require_compression_runtime_allowed,
    require_dev_only_runtime,
    require_explicit_netuid,
    require_mainnet_validator_policy,
    require_serving_stage_allowed,
    resolve_chain_identity,
    uses_mainnet_consensus_policy,
)


class _FakeCls:
    """Minimal class-with-add_args surface used by add_args / config()."""

    @classmethod
    def add_args(cls, parser: argparse.ArgumentParser) -> None:
        add_args(cls, parser)


class TestAddArgs:
    def test_base_args_registered(self) -> None:
        parser = argparse.ArgumentParser()
        add_args(_FakeCls, parser)
        dests = {a.dest for a in parser._actions}
        assert "netuid" in dests
        assert "neuron.epoch_length" in dests
        assert "endure.active_schema" in dests
        assert "endure.serving_stage" in dests
        assert {
            "neuron.device",
            "wandb.off",
            "wandb.offline",
            "wandb.notes",
            "wandb.project_name",
            "wandb.entity",
        }.isdisjoint(dests)

    def test_miner_args_registered(self) -> None:
        parser = argparse.ArgumentParser()
        add_miner_args(_FakeCls, parser)
        dests = {a.dest for a in parser._actions}
        assert "neuron.name" in dests
        assert "runtime.mode" in dests
        assert "blacklist.force_validator_permit" in dests
        assert "blacklist.allow_non_registered" in dests

    def test_epoch_length_rejects_non_positive(self) -> None:
        parser = argparse.ArgumentParser()
        add_args(_FakeCls, parser)
        with pytest.raises(SystemExit):
            parser.parse_args(["--neuron.epoch_length", "0"])

    def test_num_concurrent_forwards_rejects_non_positive(self) -> None:
        parser = argparse.ArgumentParser()
        add_validator_args(_FakeCls, parser)
        with pytest.raises(SystemExit):
            parser.parse_args(["--neuron.num_concurrent_forwards", "0"])

    def test_tick_seconds_rejects_non_positive(self) -> None:
        parser = argparse.ArgumentParser()
        add_args(_FakeCls, parser)
        with pytest.raises(SystemExit):
            parser.parse_args(["--endure.tick_seconds", "-1"])

    @pytest.mark.parametrize(
        ("add_options", "removed_argv"),
        (
            (add_args, ["--neuron.device", "cpu"]),
            (add_args, ["--wandb.off"]),
            (add_args, ["--wandb.offline"]),
            (add_args, ["--wandb.notes", "note"]),
            (add_miner_args, ["--wandb.project_name", "project"]),
            (add_miner_args, ["--wandb.entity", "entity"]),
            (add_validator_args, ["--neuron.vpermit_tao_limit", "4096"]),
        ),
    )
    def test_removed_cli_options_are_rejected(
        self,
        add_options: Callable[[object, argparse.ArgumentParser], None],
        removed_argv: list[str],
    ) -> None:
        parser = argparse.ArgumentParser()
        add_options(_FakeCls, parser)

        with pytest.raises(SystemExit):
            parser.parse_args(removed_argv)


class TestConfigFactory:
    def test_returns_bt_config_with_merged_sections(self) -> None:
        cfg = config(_FakeCls)
        # bt.config returns a Config-ish namespace; assert our custom
        # fields landed and common bt sections (wallet, subtensor,
        # logging, axon) are present.
        assert hasattr(cfg, "netuid")
        assert cfg.netuid == 1
        assert hasattr(cfg, "wallet")
        assert hasattr(cfg, "subtensor")
        assert hasattr(cfg, "logging")
        assert hasattr(cfg, "axon")
        assert cfg.endure.active_schema == RISK_SCHEMA_ID
        assert (
            cfg.endure.market_data_endpoint == "wss://archive.chain.opentensor.ai:443"
        )

    def test_namespace_contract_keeps_shared_knobs_under_endure(self) -> None:
        parser = argparse.ArgumentParser()
        add_args(_FakeCls, parser)
        dests = {action.dest for action in parser._actions}

        assert {
            "endure.database_url",
            "endure.synthetic_epoch",
            "endure.fetch_delay_seconds",
            "endure.tick_seconds",
            "endure.min_miner_stake",
            "endure.max_commits_per_round",
            "endure.api_port",
            "endure.api_host",
        } <= dests

    def test_active_schema_can_select_lending(self) -> None:
        parser = argparse.ArgumentParser()
        add_args(_FakeCls, parser)

        ns = parser.parse_args(["--endure.active_schema", FORGE_LENDING_SCHEMA_ID])

        assert getattr(ns, "endure.active_schema") == FORGE_LENDING_SCHEMA_ID

    def test_active_schema_rejects_unknown_schema(self) -> None:
        parser = argparse.ArgumentParser()
        add_args(_FakeCls, parser)

        with pytest.raises(SystemExit):
            parser.parse_args(["--endure.active_schema", "unknown.schema"])

    def test_active_schema_resolver_uses_risk_activation_default(self) -> None:
        cfg = config(_FakeCls)

        assert active_schema_id(cfg) == RISK_SCHEMA_ID

    def test_active_schema_resolver_returns_selected_lending_schema(self) -> None:
        cfg = config(_FakeCls)
        cfg.endure.active_schema = FORGE_LENDING_SCHEMA_ID

        assert active_schema_id(cfg) == FORGE_LENDING_SCHEMA_ID

    def test_active_runtime_schema_uses_served_risk_default(self) -> None:
        cfg = config(_FakeCls)

        assert active_runtime_schema_id(cfg) == RISK_SCHEMA_ID

    def test_active_runtime_schema_rejects_registered_unserved_lending(self) -> None:
        cfg = config(_FakeCls)
        cfg.endure.active_schema = FORGE_LENDING_SCHEMA_ID

        with pytest.raises(RuntimeError, match="registered_unserved"):
            active_runtime_schema_id(cfg)

    def test_dev_override_admits_unserved_lending_only_on_mock(self) -> None:
        cfg = config(_FakeCls)
        cfg.runtime = argparse.Namespace(mode="mock")
        cfg.endure.active_schema = FORGE_LENDING_SCHEMA_ID
        cfg.endure.allow_unserved_schema_for_dev = True

        assert active_runtime_schema_id(cfg) == FORGE_LENDING_SCHEMA_ID

    @pytest.mark.parametrize(
        "endpoint",
        (
            "wss://test.finney.opentensor.ai:443",
            "wss://entrypoint-finney.opentensor.ai:443",
        ),
    )
    def test_dev_override_refuses_testnet_and_mainnet(self, endpoint: str) -> None:
        cfg = config(_FakeCls)
        cfg.runtime = argparse.Namespace(mode="live")
        cfg.subtensor.chain_endpoint = endpoint
        cfg.endure.active_schema = FORGE_LENDING_SCHEMA_ID
        cfg.endure.allow_unserved_schema_for_dev = True

        with pytest.raises(RuntimeError, match="dev-only"):
            active_runtime_schema_id(cfg)

    def test_dev_guard_refuses_unset_endpoint(self) -> None:
        cfg = config(_FakeCls)
        cfg.runtime = argparse.Namespace(mode="live")
        cfg.mock = False
        cfg.subtensor.chain_endpoint = ""
        cfg.subtensor.network = ""

        assert permits_dev_only_runtime(cfg) is False
        with pytest.raises(
            RuntimeError,
            match="mock or ws://127\\.0\\.0\\.1",
        ):
            require_dev_only_runtime(cfg, feature="devnet compression")

    @pytest.mark.parametrize("network", ("finney", "test"))
    def test_dev_guard_refuses_live_network_names(self, network: str) -> None:
        cfg = config(_FakeCls)
        cfg.runtime = argparse.Namespace(mode="live")
        cfg.subtensor.chain_endpoint = ""
        cfg.subtensor.network = network

        assert permits_dev_only_runtime(cfg) is False

    def test_dev_guard_requires_localhost_hostname_equality(self) -> None:
        cfg = config(_FakeCls)
        cfg.runtime = argparse.Namespace(mode="live")
        cfg.subtensor.chain_endpoint = "ws://localhost.evil.com:9944"

        assert permits_dev_only_runtime(cfg) is False

    @pytest.mark.parametrize(
        "endpoint",
        ("ws://localhost:9944", "localhost:9944"),
    )
    def test_dev_guard_allows_local_endpoints(self, endpoint: str) -> None:
        cfg = config(_FakeCls)
        cfg.runtime = argparse.Namespace(mode="live")
        cfg.subtensor.chain_endpoint = endpoint
        cfg.subtensor.network = endpoint

        assert permits_dev_only_runtime(cfg) is True

    @pytest.mark.parametrize("mode", ("mock", "live"))
    def test_compression_guard_allows_existing_dev_paths(self, mode: str) -> None:
        cfg = config(_FakeCls)
        cfg.runtime = argparse.Namespace(mode=mode)
        cfg.subtensor.chain_endpoint = "ws://127.0.0.1:9946"
        cfg.subtensor.network = "ws://127.0.0.1:9946"

        require_compression_runtime_allowed(cfg)

    @pytest.mark.parametrize(
        ("network", "endpoint"),
        (
            ("test", ""),
            ("", "wss://test.finney.opentensor.ai:443"),
        ),
    )
    def test_compression_guard_allows_testnet_with_stage_ack(
        self, network: str, endpoint: str
    ) -> None:
        cfg = config(_FakeCls)
        cfg.runtime = argparse.Namespace(mode="live")
        cfg.endure.serving_stage = "testnet"
        cfg.subtensor.network = network
        cfg.subtensor.chain_endpoint = endpoint

        require_compression_runtime_allowed(cfg)

    def test_compression_guard_refuses_testnet_without_stage_ack(self) -> None:
        cfg = config(_FakeCls)
        cfg.runtime = argparse.Namespace(mode="live")
        cfg.subtensor.network = "test"
        cfg.subtensor.chain_endpoint = ""

        with pytest.raises(RuntimeError, match="--endure.serving_stage testnet"):
            require_compression_runtime_allowed(cfg)

    @pytest.mark.parametrize(
        ("network", "endpoint"),
        (
            ("finney", ""),
            ("", "wss://entrypoint-finney.opentensor.ai:443"),
        ),
    )
    def test_compression_guard_refuses_mainnet_even_with_testnet_stage_ack(
        self, network: str, endpoint: str
    ) -> None:
        cfg = config(_FakeCls)
        cfg.runtime = argparse.Namespace(mode="live")
        cfg.endure.serving_stage = "testnet"
        cfg.subtensor.network = network
        cfg.subtensor.chain_endpoint = endpoint

        with pytest.raises(RuntimeError, match="mainnet compression is always refused"):
            require_compression_runtime_allowed(cfg)

    def test_compression_guard_refuses_mainnet_even_with_mainnet_stage_ack(
        self,
    ) -> None:
        cfg = config(_FakeCls)
        cfg.runtime = argparse.Namespace(mode="live")
        cfg.endure.serving_stage = "mainnet"
        cfg.subtensor.network = "finney"
        cfg.subtensor.chain_endpoint = ""

        with pytest.raises(RuntimeError, match="mainnet compression is always refused"):
            require_compression_runtime_allowed(cfg)

    def test_other_dev_only_features_remain_refused_on_testnet(self) -> None:
        cfg = config(_FakeCls)
        cfg.runtime = argparse.Namespace(mode="live")
        cfg.endure.serving_stage = "testnet"
        cfg.subtensor.network = "test"
        cfg.subtensor.chain_endpoint = ""

        with pytest.raises(RuntimeError, match="dev-only"):
            require_dev_only_runtime(
                cfg, feature="--endure.allow_unserved_schema_for_dev"
            )

    def test_active_runtime_schema_admits_served_registry_entry(self) -> None:
        cfg = config(_FakeCls)
        cfg.endure.active_schema = FORGE_LENDING_SCHEMA_ID
        registry = SchemaRegistry()
        registry.register(
            SchemaRegistryEntry(
                schema=build_lending_v1_subnet_asset_schema(),
                bundle_model=LendingSubmissionBundle,
                serving_status="served",
            )
        )

        entry = active_runtime_schema_entry(cfg, registry)

        assert entry.schema.schema_id == FORGE_LENDING_SCHEMA_ID
        assert active_runtime_schema_id(cfg, registry) == FORGE_LENDING_SCHEMA_ID

    def test_serving_stage_guard_allows_mock_for_risk_schema(self) -> None:
        cfg = config(_FakeCls)
        cfg.runtime = argparse.Namespace(mode="mock")
        cfg.endure.active_schema = RISK_SCHEMA_ID

        require_serving_stage_allowed(cfg)

    def test_serving_stage_guard_allows_localhost_for_risk_schema(self) -> None:
        cfg = config(_FakeCls)
        cfg.runtime = argparse.Namespace(mode="live")
        cfg.endure.active_schema = RISK_SCHEMA_ID
        cfg.subtensor.chain_endpoint = "ws://127.0.0.1:9946"
        cfg.subtensor.network = "ws://127.0.0.1:9946"

        require_serving_stage_allowed(cfg)

    @pytest.mark.parametrize(
        ("network", "endpoint"),
        (
            ("test", ""),
            ("", "wss://test.finney.opentensor.ai:443"),
        ),
    )
    def test_serving_stage_guard_refuses_testnet_without_flag(
        self, network: str, endpoint: str
    ) -> None:
        cfg = config(_FakeCls)
        cfg.runtime = argparse.Namespace(mode="live")
        cfg.endure.active_schema = RISK_SCHEMA_ID
        cfg.subtensor.network = network
        cfg.subtensor.chain_endpoint = endpoint

        with pytest.raises(RuntimeError, match="--endure.serving_stage testnet"):
            require_serving_stage_allowed(cfg)

    @pytest.mark.parametrize(
        ("network", "endpoint"),
        (
            ("test", ""),
            ("", "wss://test.finney.opentensor.ai:443"),
        ),
    )
    def test_serving_stage_guard_allows_testnet_with_flag(
        self, network: str, endpoint: str
    ) -> None:
        cfg = config(_FakeCls)
        cfg.runtime = argparse.Namespace(mode="live")
        cfg.endure.active_schema = RISK_SCHEMA_ID
        cfg.endure.serving_stage = "testnet"
        cfg.subtensor.network = network
        cfg.subtensor.chain_endpoint = endpoint

        require_serving_stage_allowed(cfg)

    @pytest.mark.parametrize(
        "endpoint",
        ("", "wss://entrypoint-finney.opentensor.ai:443"),
    )
    def test_serving_stage_guard_allows_keyed_testnet_rpc_as_network_url(
        self, endpoint: str
    ) -> None:
        cfg = config(_FakeCls)
        cfg.runtime = argparse.Namespace(mode="live")
        cfg.endure.active_schema = RISK_SCHEMA_ID
        cfg.endure.serving_stage = "testnet"
        cfg.subtensor.network = "wss://api-bittensor-testnet.n.dwellir.com/some-key"
        cfg.subtensor.chain_endpoint = endpoint

        require_serving_stage_allowed(cfg)

    def test_serving_stage_guard_refuses_keyed_testnet_rpc_without_flag(self) -> None:
        cfg = config(_FakeCls)
        cfg.runtime = argparse.Namespace(mode="live")
        cfg.endure.active_schema = RISK_SCHEMA_ID
        cfg.subtensor.network = "wss://api-bittensor-testnet.n.dwellir.com/some-key"
        cfg.subtensor.chain_endpoint = ""

        with pytest.raises(RuntimeError, match="--endure.serving_stage testnet"):
            require_serving_stage_allowed(cfg)

    def test_serving_stage_guard_refuses_keyed_mainnet_rpc_as_network_url(self) -> None:
        cfg = config(_FakeCls)
        cfg.runtime = argparse.Namespace(mode="live")
        cfg.endure.active_schema = RISK_SCHEMA_ID
        cfg.endure.serving_stage = "testnet"
        cfg.subtensor.network = "wss://api-bittensor-mainnet.n.dwellir.com/some-key"
        cfg.subtensor.chain_endpoint = ""

        with pytest.raises(RuntimeError, match="serving_stage mainnet"):
            require_serving_stage_allowed(cfg)

    @pytest.mark.parametrize(
        ("network", "endpoint"),
        (
            ("finney", ""),
            ("", "wss://entrypoint-finney.opentensor.ai:443"),
        ),
    )
    def test_serving_stage_guard_refuses_finney_even_with_testnet_flag(
        self, network: str, endpoint: str
    ) -> None:
        cfg = config(_FakeCls)
        cfg.runtime = argparse.Namespace(mode="live")
        cfg.endure.active_schema = RISK_SCHEMA_ID
        cfg.endure.serving_stage = "testnet"
        cfg.subtensor.network = network
        cfg.subtensor.chain_endpoint = endpoint

        with pytest.raises(RuntimeError, match="serving_stage mainnet"):
            require_serving_stage_allowed(cfg)

    @pytest.mark.parametrize(
        ("network", "endpoint"),
        (
            ("finney", ""),
            ("archive", ""),
            ("latent-lite", ""),
            ("", "wss://entrypoint-finney.opentensor.ai:443"),
            ("", "wss://archive.chain.opentensor.ai:443"),
            ("", "wss://lite.sub.latent.to:443"),
            ("wss://api-bittensor-mainnet.n.dwellir.com/some-key", ""),
        ),
    )
    def test_serving_stage_guard_allows_mainnet_with_mainnet_flag(
        self, network: str, endpoint: str
    ) -> None:
        cfg = config(_FakeCls)
        cfg.runtime = argparse.Namespace(mode="live")
        cfg.endure.active_schema = RISK_SCHEMA_ID
        cfg.endure.serving_stage = "mainnet"
        cfg.subtensor.network = network
        cfg.subtensor.chain_endpoint = endpoint

        require_serving_stage_allowed(cfg)

    def test_serving_stage_guard_refuses_mainnet_without_flag(self) -> None:
        cfg = config(_FakeCls)
        cfg.runtime = argparse.Namespace(mode="live")
        cfg.endure.active_schema = RISK_SCHEMA_ID
        cfg.subtensor.network = "finney"
        cfg.subtensor.chain_endpoint = ""

        with pytest.raises(RuntimeError, match="serving_stage mainnet"):
            require_serving_stage_allowed(cfg)

    def test_serving_stage_guard_refuses_testnet_endpoint_with_mainnet_flag(
        self,
    ) -> None:
        cfg = config(_FakeCls)
        cfg.runtime = argparse.Namespace(mode="live")
        cfg.endure.active_schema = RISK_SCHEMA_ID
        cfg.endure.serving_stage = "mainnet"
        cfg.subtensor.network = "test"
        cfg.subtensor.chain_endpoint = ""

        with pytest.raises(RuntimeError, match="serving_stage testnet"):
            require_serving_stage_allowed(cfg)

    def test_serving_stage_guard_refuses_unknown_remote_endpoint(self) -> None:
        cfg = config(_FakeCls)
        cfg.runtime = argparse.Namespace(mode="live")
        cfg.endure.active_schema = RISK_SCHEMA_ID
        cfg.subtensor.network = ""
        cfg.subtensor.chain_endpoint = "ws://validator.example.net:9944"

        with pytest.raises(
            RuntimeError, match="recognized Bittensor testnet or mainnet endpoint"
        ):
            require_serving_stage_allowed(cfg)


class TestCheckConfig:
    def test_creates_neuron_full_path(self, tmp_path: Path) -> None:
        cfg = config(_FakeCls)
        cfg.logging.logging_dir = str(tmp_path)
        cfg.wallet.name = "test-cold"
        cfg.wallet.hotkey = "test-hot"
        cfg.netuid = 7
        cfg.neuron.name = "pytest-neuron"

        check_config(_FakeCls, cfg)

        expected = tmp_path / "test-cold" / "test-hot" / "netuid7" / "pytest-neuron"
        assert expected.is_dir()
        assert Path(cfg.neuron.full_path) == expected

    def test_idempotent_when_directory_already_exists(self, tmp_path: Path) -> None:
        cfg = config(_FakeCls)
        cfg.logging.logging_dir = str(tmp_path)
        cfg.wallet.name = "cold"
        cfg.wallet.hotkey = "hot"
        cfg.netuid = 1
        cfg.neuron.name = "n"

        check_config(_FakeCls, cfg)
        check_config(_FakeCls, cfg)

    def test_dead_options_are_accepted_ignored_and_warned(self, tmp_path: Path) -> None:
        parser = argparse.ArgumentParser()
        bt.Wallet.add_args(parser)
        bt.Subtensor.add_args(parser)
        bt.logging.add_args(parser)
        add_args(_FakeCls, parser)
        add_validator_args(_FakeCls, parser)
        # Values the removed options once refused (a negative delay) parse too.
        cfg = bt.Config(
            parser,
            args=[
                "--endure.fetch_delay_seconds",
                "-1",
                "--neuron.events_retention_size",
                "4096",
                "--neuron.dont_save_events",
                "--neuron.moving_average_alpha",
                "0.25",
                "--logging.logging_dir",
                str(tmp_path),
            ],
        )

        with patch.object(bt.logging, "warning") as warning:
            check_config(_FakeCls, cfg)

        assert sorted(
            call.args[0].split(" is ignored")[0] for call in warning.call_args_list
        ) == [
            "--endure.fetch_delay_seconds -1",
            "--neuron.dont_save_events",
            "--neuron.events_retention_size 4096",
            "--neuron.moving_average_alpha 0.25",
        ]
        assert not (Path(cfg.neuron.full_path) / "events.log").exists()

    def test_absent_dead_options_log_nothing(self, tmp_path: Path) -> None:
        cfg = config(_FakeCls)
        cfg.logging.logging_dir = str(tmp_path)

        with patch.object(bt.logging, "warning") as warning:
            check_config(_FakeCls, cfg)

        warning.assert_not_called()

    def test_compression_check_config_allows_testnet_with_stage_ack(
        self, tmp_path: Path
    ) -> None:
        cfg = config(_FakeCls)
        cfg.logging.logging_dir = str(tmp_path)
        cfg.wallet.name = "cold"
        cfg.wallet.hotkey = "hot"
        cfg.neuron.name = "n"
        cfg.runtime = argparse.Namespace(mode="live")
        cfg.endure.devnet_time_compression = True
        cfg.endure.serving_stage = "testnet"
        cfg.subtensor.network = "test"

        check_config(_FakeCls, cfg)

        assert Path(cfg.neuron.full_path).is_dir()

    def test_compression_check_config_refuses_testnet_without_stage_ack(
        self, tmp_path: Path
    ) -> None:
        cfg = config(_FakeCls)
        cfg.logging.logging_dir = str(tmp_path)
        cfg.wallet.name = "cold"
        cfg.wallet.hotkey = "hot"
        cfg.neuron.name = "n"
        cfg.runtime = argparse.Namespace(mode="live")
        cfg.endure.devnet_time_compression = True
        cfg.endure.serving_stage = None
        cfg.subtensor.network = "test"

        with pytest.raises(
            RuntimeError,
            match=(
                r"--endure.serving_stage testnet.*"
                r"wss://test\.finney\.opentensor\.ai:443"
            ),
        ):
            check_config(_FakeCls, cfg)


class TestArgValidation:
    def _base_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser()
        add_args(_FakeCls, parser)
        return parser

    def _validator_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser()
        add_args(_FakeCls, parser)
        add_validator_args(_FakeCls, parser)
        return parser

    def _miner_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser()
        add_args(_FakeCls, parser)
        add_miner_args(_FakeCls, parser)
        return parser

    def test_min_miner_stake_parses_decimal(self) -> None:
        ns = self._validator_parser().parse_args(["--endure.min_miner_stake", "10"])
        value = getattr(ns, "endure.min_miner_stake")
        assert value == Decimal("10")
        assert isinstance(value, Decimal)

    @pytest.mark.parametrize("bad", ["-5", "nan", "Infinity", "notanumber"])
    def test_min_miner_stake_rejects_invalid(self, bad: str) -> None:
        # A bad stake threshold must fail at boot, not on the first inbound
        # synapse the validator tries to blacklist.
        with pytest.raises(SystemExit):
            self._validator_parser().parse_args(["--endure.min_miner_stake", bad])

    def test_min_validator_stake_weight_default_is_zero_decimal(self) -> None:
        value = getattr(
            self._miner_parser().parse_args([]),
            "endure.min_validator_stake_weight",
        )

        assert value == Decimal("0")
        assert isinstance(value, Decimal)

    def test_min_validator_stake_weight_parses_boundary_decimal(self) -> None:
        namespace = self._miner_parser().parse_args(
            ["--endure.min_validator_stake_weight", "1000"]
        )
        value = getattr(namespace, "endure.min_validator_stake_weight")

        assert value == Decimal("1000")
        assert isinstance(value, Decimal)

    @pytest.mark.parametrize("bad", ["-5", "nan", "Infinity", "notanumber"])
    def test_min_validator_stake_weight_rejects_invalid(self, bad: str) -> None:
        with pytest.raises(SystemExit):
            self._miner_parser().parse_args(
                ["--endure.min_validator_stake_weight", bad]
            )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])


class TestNumericArgGuards:
    def test_endure_max_commits_per_round_rejects_non_positive(self) -> None:
        # 0 silently rejects every commit as RATE_LIMITED — must fail at parse.
        parser = argparse.ArgumentParser()
        add_args(_FakeCls, parser)
        with pytest.raises(SystemExit):
            parser.parse_args(["--endure.max_commits_per_round", "0"])

    def test_dead_template_args_are_removed(self) -> None:
        # sample_size/timeout are consumed nowhere — keeping them invites
        # configuring a knob that does nothing.
        parser = argparse.ArgumentParser()
        add_validator_args(_FakeCls, parser)
        dests = {a.dest for a in parser._actions}
        assert "neuron.sample_size" not in dests
        assert "neuron.timeout" not in dests


class TestRequireExplicitNetuid:
    @staticmethod
    def _parsed(args: list[str]) -> bt.Config:
        parser = argparse.ArgumentParser()
        bt.Wallet.add_args(parser)
        bt.Subtensor.add_args(parser)
        bt.logging.add_args(parser)
        bt.Axon.add_args(parser)
        add_args(None, parser)
        add_validator_args(None, parser)
        return bt.Config(parser, args=args)

    _LIVE = ["--runtime.mode", "live", "--subtensor.network", "finney"]

    def test_mock_runtime_accepts_the_default(self) -> None:
        require_explicit_netuid(self._parsed(["--runtime.mode", "mock"]))

    def test_local_chain_accepts_the_default(self) -> None:
        # Keyed as --subtensor.network: bittensor's resolution drops
        # --subtensor.chain_endpoint (see TESTNET_HOSTS in consensus_policy).
        cfg = self._parsed(
            ["--runtime.mode", "live", "--subtensor.network", "ws://127.0.0.1:9944"]
        )
        require_explicit_netuid(cfg)

    @pytest.mark.parametrize("form", (["--netuid", "1"], ["--netuid=1"]))
    def test_live_network_accepts_an_explicit_netuid(self, form: list[str]) -> None:
        require_explicit_netuid(self._parsed([*form, *self._LIVE]))

    def test_live_network_refuses_the_default(self) -> None:
        with pytest.raises(RuntimeError, match="pass --netuid explicitly"):
            require_explicit_netuid(self._parsed(self._LIVE))

    def test_explicitness_survives_merge_into_a_freshly_built_config(self) -> None:
        # BaseNeuron builds its own config and merges the supplied one in.
        built = self._parsed([])
        built.merge(self._parsed(["--netuid", "1", *self._LIVE]))
        require_explicit_netuid(built)


class TestPinnedConsensusSettings:
    _STALE = (
        ("endure", "min_miner_stake", Decimal("1"), Decimal("0")),
        ("endure", "max_commits_per_round", 1000, 10),
        ("endure", "max_reveals_per_round", 1000, 10),
        ("neuron", "epoch_length", 360, 100),
    )

    @classmethod
    def _stale(cls, cfg: bt.Config) -> bt.Config:
        for section, option, given, _protocol in cls._STALE:
            setattr(getattr(cfg, section), option, given)
        return cfg

    @pytest.mark.parametrize(
        ("network", "stage"), (("finney", "mainnet"), ("test", "testnet"))
    )
    def test_served_networks_ignore_stale_values_and_warn(
        self, production_validator_config: bt.Config, network: str, stage: str
    ) -> None:
        cfg = self._stale(production_validator_config)
        cfg.subtensor.network = network
        cfg.endure.serving_stage = stage

        with patch.object(bt.logging, "warning") as warning:
            apply_consensus_settings(cfg)
            require_mainnet_validator_policy(cfg)

        messages = [call.args[0] for call in warning.call_args_list]
        assert len(messages) == len(self._STALE)
        for (section, option, given, protocol), message in zip(
            self._STALE, messages, strict=True
        ):
            assert getattr(getattr(cfg, section), option) == protocol
            assert f"--{section}.{option} {given} is ignored" in message
            assert f"protocol value {protocol}" in message

    def test_protocol_values_log_nothing(
        self, production_validator_config: bt.Config
    ) -> None:
        cfg = production_validator_config
        cfg.subtensor.network = "finney"
        cfg.endure.serving_stage = "mainnet"

        with patch.object(bt.logging, "warning") as warning:
            apply_consensus_settings(cfg)

        warning.assert_not_called()

    @pytest.mark.parametrize(
        "network", ("ws://127.0.0.1:9944", "local"), ids=("loopback", "local")
    )
    def test_local_chains_keep_custom_values(
        self, production_validator_config: bt.Config, network: str
    ) -> None:
        cfg = self._stale(production_validator_config)
        cfg.subtensor.network = network

        apply_consensus_settings(cfg)

        for section, option, given, _protocol in self._STALE:
            assert getattr(getattr(cfg, section), option) == given

    def test_axon_off_requires_emission_disabled_on_mainnet(
        self, production_validator_config: bt.Config
    ) -> None:
        cfg = production_validator_config
        cfg.subtensor.network = "finney"
        cfg.endure.serving_stage = "mainnet"
        cfg.neuron.axon_off = True
        cfg.neuron.disable_set_weights = True
        require_mainnet_validator_policy(cfg)

        cfg.neuron.disable_set_weights = False

        with pytest.raises(RuntimeError, match="disable_set_weights"):
            require_mainnet_validator_policy(cfg)


class TestChainIdentityByGenesis:
    """An operator's own Finney node on loopback is mainnet, not a dev chain."""

    # The SDK resolves each of these to ws://127.0.0.1:9944 ("finney" would
    # override a loopback chain_endpoint, so it is not a loopback case).
    _LOOPBACK = (
        ("local", ""),
        ("", "ws://127.0.0.1:9944"),
        ("ws://127.0.0.1:9944", ""),
    )

    @staticmethod
    def _reader(genesis: str | None) -> Callable[[str], str | None]:
        def read(endpoint: str) -> str | None:
            assert "127.0.0.1" in endpoint
            return genesis

        return read

    @pytest.mark.parametrize(("network", "endpoint"), _LOOPBACK)
    def test_loopback_mainnet_node_gets_every_mainnet_gate(
        self, production_validator_config: bt.Config, network: str, endpoint: str
    ) -> None:
        cfg = production_validator_config
        cfg.subtensor.network = network
        cfg.subtensor.chain_endpoint = endpoint
        cfg.endure.serving_stage = "mainnet"
        cfg.endure.min_miner_stake = Decimal("5")
        assert permits_dev_only_runtime(cfg)

        resolve_chain_identity(cfg, read_genesis=self._reader(MAINNET_GENESIS_HASH))

        assert not permits_dev_only_runtime(cfg)
        assert uses_mainnet_consensus_policy(cfg)
        assert owner_vote_network(cfg) == "mainnet"
        apply_consensus_settings(cfg)
        assert cfg.endure.min_miner_stake == Decimal("0")
        cfg.endure.serving_stage = "testnet"
        with pytest.raises(DevOnlyConfigError):
            require_serving_stage_allowed(cfg)
        with pytest.raises(DevOnlyConfigError):
            require_dev_only_runtime(cfg, feature="--endure.devnet_time_compression")

    @pytest.mark.parametrize(("network", "endpoint"), _LOOPBACK)
    def test_loopback_testnet_node_is_testnet(
        self, production_validator_config: bt.Config, network: str, endpoint: str
    ) -> None:
        cfg = production_validator_config
        cfg.subtensor.network = network
        cfg.subtensor.chain_endpoint = endpoint

        resolve_chain_identity(cfg, read_genesis=self._reader(TESTNET_GENESIS_HASH))

        assert not permits_dev_only_runtime(cfg)
        assert not uses_mainnet_consensus_policy(cfg)
        assert owner_vote_network(cfg) == "testnet"

    def test_loopback_localnet_stays_a_dev_chain(
        self, production_validator_config: bt.Config
    ) -> None:
        cfg = production_validator_config
        cfg.subtensor.network = "local"
        cfg.subtensor.chain_endpoint = ""

        resolve_chain_identity(cfg, read_genesis=self._reader("0xlocalnet"))

        assert permits_dev_only_runtime(cfg)
        assert owner_vote_network(cfg) is None

    @pytest.mark.parametrize("runtime", ["named-finney", "mock"])
    def test_named_networks_and_mock_never_read_the_chain(
        self, production_validator_config: bt.Config, runtime: str
    ) -> None:
        cfg = production_validator_config
        if runtime == "mock":
            cfg.runtime.mode = "mock"
            cfg.subtensor.network = "local"
        else:
            cfg.subtensor.network = "finney"
        cfg.subtensor.chain_endpoint = ""

        def unexpected(_endpoint: str) -> str | None:
            raise AssertionError("genesis read")

        resolve_chain_identity(cfg, read_genesis=unexpected)

    def test_unidentifiable_chain_refuses_startup(
        self, production_validator_config: bt.Config
    ) -> None:
        cfg = production_validator_config
        cfg.subtensor.network = "local"
        cfg.subtensor.chain_endpoint = ""

        with pytest.raises(RuntimeError, match="cannot identify the chain"):
            resolve_chain_identity(cfg, read_genesis=self._reader(None))

    def test_preseeded_genesis_never_overrides_a_named_endpoint(
        self, production_validator_config: bt.Config
    ) -> None:
        cfg = production_validator_config
        cfg.subtensor.network = "finney"
        cfg.subtensor.chain_endpoint = ""
        # e.g. carried in through a YAML --config file.
        cfg.endure.chain_genesis_hash = TESTNET_GENESIS_HASH

        resolve_chain_identity(cfg, read_genesis=self._reader(None))

        assert owner_vote_network(cfg) == "mainnet"
        assert uses_mainnet_consensus_policy(cfg)

    def test_genesis_hex_is_normalized_before_comparison(
        self, production_validator_config: bt.Config
    ) -> None:
        cfg = production_validator_config
        cfg.subtensor.network = "local"
        cfg.subtensor.chain_endpoint = ""

        resolve_chain_identity(
            cfg, read_genesis=self._reader(MAINNET_GENESIS_HASH[2:].upper())
        )

        assert owner_vote_network(cfg) == "mainnet"
