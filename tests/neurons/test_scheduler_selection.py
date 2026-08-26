"""Neuron scheduler selection for Alpha Risk's 24/7 protocol clock."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import bittensor as bt
import pytest

from endure.assessment.schemas.subnet_alpha_risk import RISK_SCHEMA_ID
from endure.protocol.schedulers import FixedUtcScheduler


def test_validator_selects_fixed_utc_for_alpha_risk(
    mock_validator_config: bt.Config,
) -> None:
    from neurons.validator import Validator

    mock_validator_config.endure.active_schema = RISK_SCHEMA_ID
    mock_validator_config.endure.devnet_time_compression = False
    mock_validator_config.neuron.axon_off = True
    mock_validator_config.neuron.disable_set_weights = True

    validator = Validator(config=mock_validator_config)

    assert isinstance(validator._service._scheduler, FixedUtcScheduler)


def test_live_risk_window_callbacks_use_distinct_boundary_lookups(
    mock_validator_config: bt.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The start callback must resolve the first block at/after reveal_close
    # and the end callback the last block at/before the horizon end; wiring
    # both to the same provider method is the off-by-one this test locks out.
    import neurons.validator as validator_module
    from endure.live.alpha_market_data import LiveAlphaPriceProvider
    from endure.scoring.risk.orchestrator import RiskScoringOrchestrator

    mock_validator_config.endure.active_schema = RISK_SCHEMA_ID
    mock_validator_config.endure.devnet_time_compression = False
    mock_validator_config.neuron.axon_off = True
    mock_validator_config.neuron.disable_set_weights = True

    monkeypatch.setattr(
        validator_module, "permits_dev_only_runtime", lambda _config: False
    )
    monkeypatch.setattr(
        LiveAlphaPriceProvider,
        "block_for_reveal_close",
        lambda self, reveal_close, *, now: 3,
    )
    monkeypatch.setattr(
        LiveAlphaPriceProvider,
        "last_finalized_block_at_or_before",
        lambda self, timestamp, *, now: 7,
    )

    captured: dict[str, Any] = {}
    original_init = RiskScoringOrchestrator.__init__

    def recording_init(
        self: RiskScoringOrchestrator, *args: Any, **kwargs: Any
    ) -> None:
        captured.update(kwargs)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(RiskScoringOrchestrator, "__init__", recording_init)

    validator_module.Validator(config=mock_validator_config)

    when = datetime(2026, 7, 21, tzinfo=UTC)
    assert captured["reveal_close_block"](when) == 3
    assert captured["window_end_block"](when) == 7


def test_miner_selects_fixed_utc_for_alpha_risk(mock_miner_config: bt.Config) -> None:
    from neurons.miner import Miner

    mock_miner_config.endure.active_schema = RISK_SCHEMA_ID
    mock_miner_config.endure.devnet_time_compression = False

    miner = Miner(config=mock_miner_config)

    assert miner._round_service is not None
    assert isinstance(miner._round_service._scheduler, FixedUtcScheduler)
