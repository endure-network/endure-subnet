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
from endure.utils.config import (
    active_runtime_schema_entry,
    active_runtime_schema_id,
    active_schema_id,
    add_args,
    add_miner_args,
    add_validator_args,
    check_config,
    config,
    permits_dev_only_runtime,
    require_compression_runtime_allowed,
    require_dev_only_runtime,
    require_explicit_netuid,
    require_serving_stage_allowed,
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

    def test_validator_args_registered(self) -> None:
        parser = argparse.ArgumentParser()
        add_validator_args(_FakeCls, parser)
        dests = {a.dest for a in parser._actions}
        assert "neuron.name" in dests
        assert "runtime.mode" in dests
        assert "neuron.disable_set_weights" in dests
        assert "neuron.moving_average_alpha" in dests
        assert "neuron.vpermit_tao_limit" not in dests

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

        with pytest.raises(RuntimeError, match="R7 soak gate"):
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

        with pytest.raises(RuntimeError, match="R7 soak gate"):
            require_serving_stage_allowed(cfg)

    def test_serving_stage_guard_refuses_unknown_remote_endpoint(self) -> None:
        cfg = config(_FakeCls)
        cfg.runtime = argparse.Namespace(mode="live")
        cfg.endure.active_schema = RISK_SCHEMA_ID
        cfg.subtensor.network = ""
        cfg.subtensor.chain_endpoint = "ws://validator.example.net:9944"

        with pytest.raises(
            RuntimeError, match="mainnet serving requires a code change"
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
        cfg.neuron.dont_save_events = True

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
        cfg.neuron.dont_save_events = True

        check_config(_FakeCls, cfg)
        check_config(_FakeCls, cfg)

    def test_registers_events_logger_when_enabled(self, tmp_path: Path) -> None:
        cfg = config(_FakeCls)
        cfg.logging.logging_dir = str(tmp_path)
        cfg.wallet.name = "cold"
        cfg.wallet.hotkey = "hot"
        cfg.netuid = 1
        cfg.neuron.name = "n"
        cfg.neuron.dont_save_events = False
        cfg.neuron.events_retention_size = 2048

        with patch.object(bt.logging, "register_primary_logger") as registered:
            check_config(_FakeCls, cfg)
            registered.assert_called_once()

    def test_compression_check_config_allows_testnet_with_stage_ack(
        self, tmp_path: Path
    ) -> None:
        cfg = config(_FakeCls)
        cfg.logging.logging_dir = str(tmp_path)
        cfg.wallet.name = "cold"
        cfg.wallet.hotkey = "hot"
        cfg.neuron.name = "n"
        cfg.neuron.dont_save_events = True
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
        cfg.neuron.dont_save_events = True
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

    def test_events_retention_size_parses_positive_int(self) -> None:
        ns = self._base_parser().parse_args(["--neuron.events_retention_size", "4096"])
        value = getattr(ns, "neuron.events_retention_size")
        assert value == 4096
        assert isinstance(value, int)

    def test_events_retention_size_default_is_positive_int(self) -> None:
        value = getattr(
            self._base_parser().parse_args([]), "neuron.events_retention_size"
        )
        assert isinstance(value, int)
        assert value > 0

    @pytest.mark.parametrize("bad", ["0", "-5", "notanint"])
    def test_events_retention_size_rejects_invalid(self, bad: str) -> None:
        with pytest.raises(SystemExit):
            self._base_parser().parse_args(["--neuron.events_retention_size", bad])

    def test_moving_average_alpha_parses_decimal(self) -> None:
        ns = self._validator_parser().parse_args(
            ["--neuron.moving_average_alpha", "0.25"]
        )
        value = getattr(ns, "neuron.moving_average_alpha")
        assert value == Decimal("0.25")
        assert isinstance(value, Decimal)

    def test_moving_average_alpha_default_is_unit_interval_decimal(self) -> None:
        value = getattr(
            self._validator_parser().parse_args([]), "neuron.moving_average_alpha"
        )
        assert isinstance(value, Decimal)
        assert Decimal("0") <= value <= Decimal("1")

    @pytest.mark.parametrize("bad", ["1.5", "-0.1", "nan", "notanumber"])
    def test_moving_average_alpha_rejects_out_of_range(self, bad: str) -> None:
        with pytest.raises(SystemExit):
            self._validator_parser().parse_args(["--neuron.moving_average_alpha", bad])

    def test_min_miner_stake_parses_decimal(self) -> None:
        ns = self._validator_parser().parse_args(["--endure.min_miner_stake", "10"])
        value = getattr(ns, "endure.min_miner_stake")
        assert value == Decimal("10")
        assert isinstance(value, Decimal)

    def test_min_miner_stake_default_is_zero_decimal(self) -> None:
        value = getattr(
            self._validator_parser().parse_args([]), "endure.min_miner_stake"
        )
        assert isinstance(value, Decimal)
        assert value == Decimal("0")

    @pytest.mark.parametrize("bad", ["-5", "nan", "notanumber"])
    def test_min_miner_stake_rejects_invalid(self, bad: str) -> None:
        # A bad stake threshold must fail at boot, not on the first inbound
        # synapse the validator tries to blacklist.
        with pytest.raises(SystemExit):
            self._validator_parser().parse_args(["--endure.min_miner_stake", bad])


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])


class TestNumericArgGuards:
    def test_endure_max_commits_per_round_rejects_non_positive(self) -> None:
        # 0 silently rejects every commit as RATE_LIMITED — must fail at parse.
        parser = argparse.ArgumentParser()
        add_args(_FakeCls, parser)
        with pytest.raises(SystemExit):
            parser.parse_args(["--endure.max_commits_per_round", "0"])

    def test_endure_fetch_delay_seconds_rejects_non_positive(self) -> None:
        # Negative pulls outcome fetch before session close: unsettled data.
        parser = argparse.ArgumentParser()
        add_args(_FakeCls, parser)
        with pytest.raises(SystemExit):
            parser.parse_args(["--endure.fetch_delay_seconds", "-1"])

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
        # --subtensor.chain_endpoint (see _TESTNET_HOSTS in the module).
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
