"""The risk runtime draws pool tie seeds from the chain only when it reads one."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from endure.assessment.schemas.subnet_alpha_risk import RISK_SCHEMA_ID
from neurons import validator as validator_module
from neurons.validator import Validator
from tests.conftest import _base_config


def _validator(tmp_path: Path, runtime_mode: str, network: str = "finney") -> Validator:
    validator = Validator.__new__(Validator)
    config = _base_config(tmp_path, ["validator"], runtime_mode=runtime_mode)
    config.subtensor.network = network
    config.subtensor.chain_endpoint = ""
    config.endure.active_schema = RISK_SCHEMA_ID
    validator.config = config
    validator._storage = MagicMock()
    validator.metagraph = MagicMock(hotkeys=[])
    return validator


@pytest.mark.parametrize("network", ["finney", "test"])
def test_a_live_validator_orders_pool_ties_by_an_archive_block_hash(
    tmp_path: Path, network: str
) -> None:
    provider = MagicMock()
    with (
        patch.object(validator_module, "LiveAlphaPriceProvider", return_value=provider),
        patch.object(validator_module, "RiskScoringOrchestrator") as orchestrator,
    ):
        validator_module._build_risk_vertical_runtime(
            _validator(tmp_path, "live", network)
        )

    assert orchestrator.call_args.kwargs["tie_seed_reads"] == provider.tie_seed_reads


def test_a_mock_validator_seeds_pool_ties_with_round_ids(tmp_path: Path) -> None:
    with patch.object(validator_module, "RiskScoringOrchestrator") as orchestrator:
        validator_module._build_risk_vertical_runtime(_validator(tmp_path, "mock"))

    assert orchestrator.call_args.kwargs["tie_seed_reads"] is None


def test_a_compressed_testnet_run_seeds_pool_ties_with_round_ids(
    tmp_path: Path,
) -> None:
    validator = _validator(tmp_path, "live", "test")
    validator.config.endure.serving_stage = "testnet"
    validator.config.endure.devnet_time_compression = True
    live = MagicMock(side_effect=AssertionError("live archive built"))
    with (
        patch.object(validator_module, "LiveAlphaPriceProvider", live),
        patch.object(validator_module, "RiskScoringOrchestrator") as orchestrator,
    ):
        validator_module._build_risk_vertical_runtime(validator)

    assert orchestrator.call_args.kwargs["tie_seed_reads"] is None
