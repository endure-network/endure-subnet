import argparse
import asyncio
import logging
import threading
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import bittensor as bt
import numpy as np
import pytest
from bittensor.utils.mock.subtensor_mock import __GLOBAL_MOCK_STATE__

from endure.assessment.schemas.forge_lending import FORGE_LENDING_SCHEMA_ID
from endure.assessment.schemas.subnet_alpha_risk import RISK_SCHEMA_ID
from endure.assessment.subnet_alpha_universe import StaticAlphaRiskUniverseProvider


@contextmanager
def _patched_chain() -> Iterator[MagicMock]:
    fake_wallet = MagicMock()
    fake_wallet.hotkey.ss58_address = "5Test"
    fake_metagraph = MagicMock()
    fake_metagraph.hotkeys = ["5Test"]
    fake_metagraph.n = 1
    fake_metagraph.last_update = [0]
    fake_subtensor = MagicMock()
    fake_subtensor.is_hotkey_registered.return_value = True
    fake_subtensor.metagraph.return_value = fake_metagraph
    fake_subtensor.chain_endpoint = "ws://test-mock-endpoint"
    fake_subtensor.get_current_block.return_value = 0
    with ExitStack() as stack:
        stack.enter_context(patch("bittensor.Wallet", return_value=fake_wallet))
        stack.enter_context(patch("bittensor.Subtensor", return_value=fake_subtensor))
        stack.enter_context(patch("bittensor.Dendrite", return_value=MagicMock()))
        stack.enter_context(patch("bittensor.Axon", return_value=MagicMock()))
        yield fake_subtensor


def test_validator_bootstraps_in_mock_mode_axon_off(
    mock_validator_config: bt.Config,
    trap_external_ip: dict[str, int],
) -> None:
    from neurons.validator import Validator

    mock_validator_config.neuron.axon_off = True
    mock_validator_config.neuron.disable_set_weights = True
    state_path = (
        Path(mock_validator_config.logging.logging_dir)
        / mock_validator_config.wallet.name
        / mock_validator_config.wallet.hotkey
        / f"netuid{mock_validator_config.netuid}"
        / mock_validator_config.neuron.name
        / "state.npz"
    )
    assert not state_path.exists()

    validator = Validator(config=mock_validator_config)

    assert validator.wallet is not None
    assert not hasattr(validator, "axon")
    assert state_path.exists()
    assert trap_external_ip["count"] == 0


def test_validator_rejects_lending_selection_until_serving_gate(
    mock_validator_config: bt.Config,
) -> None:
    from neurons.validator import Validator

    mock_validator_config.endure.active_schema = FORGE_LENDING_SCHEMA_ID
    mock_validator_config.neuron.axon_off = True
    mock_validator_config.neuron.disable_set_weights = True

    with pytest.raises(RuntimeError, match="registered/selectable"):
        Validator(config=mock_validator_config)


def test_validator_boots_and_restarts_dev_admitted_lending_schema(
    mock_validator_config: bt.Config,
) -> None:
    from endure.protocol.vertical import AssessmentRoundProgram
    from neurons.validator import Validator

    mock_validator_config.endure.active_schema = FORGE_LENDING_SCHEMA_ID
    mock_validator_config.endure.allow_unserved_schema_for_dev = True
    mock_validator_config.neuron.axon_off = True
    mock_validator_config.neuron.disable_set_weights = True
    state_path = (
        Path(mock_validator_config.logging.logging_dir)
        / mock_validator_config.wallet.name
        / mock_validator_config.wallet.hotkey
        / f"netuid{mock_validator_config.netuid}"
        / mock_validator_config.neuron.name
        / "state.npz"
    )
    assert not state_path.exists()

    validator = Validator(config=mock_validator_config)

    assert validator._schema_id == FORGE_LENDING_SCHEMA_ID
    assert isinstance(validator._vertical_runtime.round_program, AssessmentRoundProgram)
    assert validator._vertical_runtime.publisher == "assessment"
    assert validator.scores == [Decimal("0")] * int(validator.metagraph.n)
    assert state_path.exists()

    validator.save_state()
    __GLOBAL_MOCK_STATE__.clear()

    restarted = Validator(config=mock_validator_config)

    assert restarted._schema_id == FORGE_LENDING_SCHEMA_ID
    assert restarted.scores == [Decimal("0")] * int(restarted.metagraph.n)


def test_validator_wires_served_risk_schema_with_devnet_compression(
    mock_validator_config: bt.Config,
) -> None:
    from neurons.validator import Validator

    mock_validator_config.endure.active_schema = RISK_SCHEMA_ID
    mock_validator_config.endure.devnet_time_compression = True
    mock_validator_config.neuron.axon_off = True
    mock_validator_config.neuron.disable_set_weights = True

    validator = Validator(config=mock_validator_config)

    assert validator._schema_id == RISK_SCHEMA_ID
    assert isinstance(
        validator._service._universe_provider, StaticAlphaRiskUniverseProvider
    )
    assert validator._service._universe_provider.fetch_universe(
        "2026-08-25"
    ).tickers == ("8", "44")


def test_validator_rejects_concurrent_risk_forwards(
    mock_validator_config: bt.Config,
) -> None:
    from neurons.validator import Validator

    mock_validator_config.endure.active_schema = RISK_SCHEMA_ID
    mock_validator_config.neuron.num_concurrent_forwards = 2
    mock_validator_config.neuron.axon_off = True
    mock_validator_config.neuron.disable_set_weights = True

    with pytest.raises(RuntimeError, match="num_concurrent_forwards"):
        Validator(config=mock_validator_config)


def test_validator_refuses_served_risk_schema_on_finney(
    production_validator_config: bt.Config,
) -> None:
    from neurons.validator import Validator

    production_validator_config.endure.active_schema = RISK_SCHEMA_ID
    production_validator_config.subtensor.chain_endpoint = (
        "wss://entrypoint-finney.opentensor.ai:443"
    )
    production_validator_config.subtensor.network = "finney"
    production_validator_config.neuron.axon_off = True
    production_validator_config.neuron.disable_set_weights = True

    with _patched_chain(), pytest.raises(RuntimeError, match="R7 soak gate"):
        Validator(config=production_validator_config)


def test_validator_refuses_defaulted_netuid_on_live_network(tmp_path: Path) -> None:
    from endure.utils.config import add_args, add_validator_args
    from neurons.validator import Validator

    parser = argparse.ArgumentParser()
    bt.Wallet.add_args(parser)
    bt.Subtensor.add_args(parser)
    bt.logging.add_args(parser)
    bt.Axon.add_args(parser)
    add_args(None, parser)
    add_validator_args(None, parser)
    # Testnet with its serving acknowledgement passes every other gate, so
    # the defaulted netuid is the only thing left to refuse.
    config = bt.Config(
        parser,
        args=[
            "--runtime.mode",
            "live",
            "--subtensor.network",
            "test",
            "--endure.serving_stage",
            "testnet",
            "--neuron.dont_save_events",
        ],
    )
    config.logging.logging_dir = str(tmp_path)

    with (
        _patched_chain(),
        pytest.raises(RuntimeError, match="pass --netuid explicitly"),
    ):
        Validator(config=config)


def test_validator_serving_gate_prevents_axon_creation_on_finney(
    production_validator_config: bt.Config,
) -> None:
    from neurons.validator import Validator

    production_validator_config.endure.active_schema = RISK_SCHEMA_ID
    production_validator_config.subtensor.chain_endpoint = (
        "wss://entrypoint-finney.opentensor.ai:443"
    )
    production_validator_config.subtensor.network = "finney"
    production_validator_config.neuron.axon_off = False
    production_validator_config.neuron.disable_set_weights = True

    with (
        _patched_chain() as subtensor,
        patch("bittensor.Axon") as create_axon,
        pytest.raises(RuntimeError, match="R7 soak gate"),
    ):
        Validator(config=production_validator_config)

    subtensor.serve_axon.assert_not_called()
    assert not any("wallet" in call.kwargs for call in create_axon.call_args_list)


def test_validator_bootstraps_in_mock_mode_dev_path(
    mock_validator_config: bt.Config,
    trap_external_ip: dict[str, int],
    caplog: pytest.LogCaptureFixture,
) -> None:
    from neurons.validator import Validator

    mock_validator_config.neuron.axon_off = False
    mock_validator_config.neuron.disable_set_weights = True

    with caplog.at_level(logging.ERROR, logger="bittensor"):
        validator = Validator(config=mock_validator_config)

    assert validator.wallet is not None
    assert validator.axon is not None
    assert validator.axon.ip == "127.0.0.1"
    assert validator.axon.external_ip == "127.0.0.1"
    assert trap_external_ip["count"] == 0
    assert "Failed to serve Axon" not in caplog.text
    assert "Subnet mechanism" not in caplog.text


def test_migrations_create_missing_sqlite_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from neurons.validator import _run_migrations

    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / "var").exists()

    _run_migrations("sqlite:///var/endure.db")

    assert (tmp_path / "var" / "endure.db").is_file()


def test_migrations_preserve_bittensor_logger(tmp_path: Path) -> None:
    from neurons.validator import _run_migrations

    logger = logging.getLogger("bittensor")
    logger.disabled = False

    _run_migrations(f"sqlite:///{tmp_path / 'preserve-logs.db'}")

    assert logger.disabled is False


def test_validator_restores_step_from_npz_but_scores_from_sqlite(
    mock_validator_config: bt.Config,
) -> None:
    from endure.assessment.coordinates import (
        AssessmentCoordinate,
        AssessmentEmaState,
    )
    from endure.assessment.schemas.subnet_alpha_risk import (
        HORIZON_5D_SECONDS,
        RiskOutput,
    )
    from endure.storage.repository import Storage
    from neurons.validator import Validator, _run_migrations

    mock_validator_config.endure.active_schema = RISK_SCHEMA_ID
    mock_validator_config.neuron.axon_off = True
    mock_validator_config.neuron.disable_set_weights = True

    _run_migrations(mock_validator_config.endure.database_url)
    storage = Storage.from_url(mock_validator_config.endure.database_url)
    for hotkey, blend in (("miner-hotkey-1", "0.3"), ("miner-hotkey-2", "0.7")):
        storage.upsert_assessment_ema(
            RISK_SCHEMA_ID,
            AssessmentEmaState(
                miner_hotkey=hotkey,
                coordinate=AssessmentCoordinate.subnet_asset(
                    netuid=44,
                    horizon_seconds=HORIZON_5D_SECONDS,
                    output=RiskOutput.MAX_DRAWDOWN.value,
                ),
                ema=Decimal(blend),
                resolved_rounds=1,
            ),
            now_iso="2026-08-13T12:00:00+00:00",
        )
    storage._engine.dispose()

    state_path = (
        Path(mock_validator_config.logging.logging_dir)
        / mock_validator_config.wallet.name
        / mock_validator_config.wallet.hotkey
        / f"netuid{mock_validator_config.netuid}"
        / mock_validator_config.neuron.name
        / "state.npz"
    )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    stale_scores = ["9.5", "8.5"]
    np.savez(
        state_path,
        step=7,
        scores=np.array(stale_scores),
        hotkeys=np.array(["miner-hotkey-1", "miner-hotkey-2"]),
    )
    __GLOBAL_MOCK_STATE__.clear()

    restored_validator = Validator(config=mock_validator_config)

    program = restored_validator._service._round_program
    assert program is not None
    expected_scores = [
        program.weights().get(hotkey, Decimal(0))
        for hotkey in restored_validator.hotkeys
    ]
    assert restored_validator.step == 7
    assert restored_validator.scores == expected_scores
    assert Decimal("9.5") not in restored_validator.scores
    assert Decimal("8.5") not in restored_validator.scores
    assert any(score > Decimal(0) for score in restored_validator.scores)


def test_blacklist_rejects_miners_below_min_stake(
    mock_validator_config: bt.Config,
) -> None:
    """Registration alone is cheap; the stake gate bounds who can impose
    commit/reveal load."""
    from types import SimpleNamespace

    from neurons.validator import Validator

    mock_validator_config.neuron.axon_off = True
    mock_validator_config.neuron.disable_set_weights = True
    mock_validator_config.endure.min_miner_stake = Decimal("10")
    validator = Validator(config=mock_validator_config)
    validator.metagraph = SimpleNamespace(hotkeys=["hk-poor", "hk-rich"], S=[1.0, 50.0])

    poor = SimpleNamespace(dendrite=SimpleNamespace(hotkey="hk-poor"))
    rich = SimpleNamespace(dendrite=SimpleNamespace(hotkey="hk-rich"))

    assert validator._blacklist(poor)[0] is True
    assert validator._blacklist(rich)[0] is False


def test_forward_tracks_tick_health(
    mock_validator_config: bt.Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stuck validator must be observable: consecutive tick failures and
    the last success surface through runtime_health()."""
    from neurons.validator import Validator

    mock_validator_config.neuron.axon_off = True
    mock_validator_config.neuron.disable_set_weights = True
    validator = Validator(config=mock_validator_config)

    async def _no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(
        validator._service, "tick", lambda **_: (_ for _ in ()).throw(OSError("boom"))
    )
    asyncio.run(validator.forward())
    asyncio.run(validator.forward())
    health = validator.runtime_health()
    assert health["consecutive_tick_failures"] == 2
    # The public endpoint surfaces the error type, never the raw message
    # (which can carry db paths / oracle URLs).
    assert health["last_tick_error"] == "OSError"
    assert "boom" not in str(health["last_tick_error"])

    monkeypatch.setattr(validator._service, "tick", lambda **_: None)
    asyncio.run(validator.forward())
    validator.thread = MagicMock()
    validator.thread.is_alive.return_value = True
    health = validator.runtime_health()
    assert health["consecutive_tick_failures"] == 0
    assert health["last_tick_ok"] is not None
    assert health["validator_loop_alive"] is True
    assert health["tick_stale"] is False


def test_failed_tick_refreshes_loop_heartbeat(
    mock_validator_config: bt.Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from neurons.validator import Validator

    mock_validator_config.neuron.axon_off = True
    mock_validator_config.neuron.disable_set_weights = True
    mock_validator_config.endure.health_startup_grace_seconds = 120
    now = [1_000.0]
    monkeypatch.setattr("neurons.validator.time.monotonic", lambda: now[0])
    validator = Validator(config=mock_validator_config)
    validator.thread = MagicMock()
    validator.thread.is_alive.return_value = True

    async def _no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(
        validator._service, "tick", lambda **_: (_ for _ in ()).throw(OSError("boom"))
    )

    now[0] = 1_015.0
    asyncio.run(validator.forward())
    now[0] = 1_016.0

    assert validator.watchdog_exit_reason() is None


def test_validator_rejects_watchdog_age_not_greater_than_tick_cadence(
    mock_validator_config: bt.Config,
) -> None:
    from neurons.validator import Validator

    mock_validator_config.neuron.axon_off = True
    mock_validator_config.neuron.disable_set_weights = True
    mock_validator_config.endure.tick_seconds = 60
    mock_validator_config.endure.health_tick_max_age_seconds = 60

    with pytest.raises(RuntimeError, match="greater than endure.tick_seconds"):
        Validator(config=mock_validator_config)


def test_validator_rejects_startup_grace_not_greater_than_tick_cadence(
    mock_validator_config: bt.Config,
) -> None:
    from neurons.validator import Validator

    mock_validator_config.neuron.axon_off = True
    mock_validator_config.neuron.disable_set_weights = True
    mock_validator_config.endure.tick_seconds = 60
    mock_validator_config.endure.health_startup_grace_seconds = 60

    with pytest.raises(RuntimeError, match="greater than endure.tick_seconds"):
        Validator(config=mock_validator_config)


def test_runtime_health_reports_dead_background_thread(
    mock_validator_config: bt.Config,
) -> None:
    from neurons.validator import Validator

    mock_validator_config.neuron.axon_off = True
    mock_validator_config.neuron.disable_set_weights = True
    validator = Validator(config=mock_validator_config)
    validator.thread = MagicMock()
    validator.thread.is_alive.return_value = False

    health = validator.runtime_health()

    assert health["validator_loop_alive"] is False
    assert validator.watchdog_exit_reason() == "validator loop thread exited"


def test_runtime_health_reports_unstarted_background_thread_as_not_alive(
    mock_validator_config: bt.Config,
) -> None:
    from neurons.validator import Validator

    mock_validator_config.neuron.axon_off = True
    mock_validator_config.neuron.disable_set_weights = True
    validator = Validator(config=mock_validator_config)

    assert validator.runtime_health()["validator_loop_alive"] is False


def test_main_stops_cleanly_when_the_shutdown_event_is_set() -> None:
    from neurons.validator import main

    validator = MagicMock()
    validator.watchdog_exit_reason.return_value = None
    context = MagicMock()
    context.__enter__.return_value = validator
    stop = threading.Event()
    stop.set()

    with (
        patch("neurons.validator.install_shutdown_handlers", return_value=stop),
        patch("neurons.validator.Validator", return_value=context),
    ):
        main()

    context.__exit__.assert_called_once()
    validator.watchdog_exit_reason.assert_not_called()


def test_main_exits_nonzero_and_cleans_up_on_watchdog_failure() -> None:
    from neurons.validator import main

    validator = MagicMock()
    validator.watchdog_exit_reason.return_value = "validator loop thread exited"
    context = MagicMock()
    context.__enter__.return_value = validator

    with (
        patch(
            "neurons.validator.install_shutdown_handlers",
            return_value=threading.Event(),
        ),
        patch("neurons.validator.Validator", return_value=context),
        pytest.raises(SystemExit) as exit_info,
    ):
        main()

    assert exit_info.value.code == 1
    context.__exit__.assert_called_once()


def test_main_redacts_runtime_endpoint_credentials() -> None:
    from neurons.validator import main

    credential_url = "".join(
        ("wss://user:password", "@rpc.example.invalid/ws?token=secret")
    )
    error_log = MagicMock()

    with (
        patch(
            "neurons.validator.install_shutdown_handlers",
            return_value=threading.Event(),
        ),
        patch("neurons.validator.Validator", side_effect=RuntimeError(credential_url)),
        patch("neurons.validator.bt.logging.error", error_log),
        pytest.raises(SystemExit) as exit_info,
    ):
        main()

    assert exit_info.value.code == 1
    rendered = str(error_log.call_args.args[0])
    assert "password" not in rendered
    assert "secret" not in rendered
    assert "<redacted-endpoint>" in rendered


def test_runtime_health_reports_stale_tick(
    mock_validator_config: bt.Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from neurons.validator import Validator

    mock_validator_config.neuron.axon_off = True
    mock_validator_config.neuron.disable_set_weights = True
    mock_validator_config.endure.health_tick_max_age_seconds = 60
    mock_validator_config.endure.health_startup_grace_seconds = 120
    monkeypatch.setattr("neurons.validator.time.monotonic", lambda: 1_000.0)
    validator = Validator(config=mock_validator_config)
    validator._last_tick_ok = "2026-07-29T00:00:00+00:00"
    validator._last_tick_monotonic = 900.0
    validator.thread = MagicMock()
    validator.thread.is_alive.return_value = True

    health = validator.runtime_health()

    assert health["tick_stale"] is True
    assert health["seconds_since_last_tick"] == 100
    assert validator.watchdog_exit_reason() == "validator tick stale"


def test_set_weights_abstains_until_first_resolution(
    mock_validator_config: bt.Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Epoch 0: all-zero scores would emit the SDK's uniform fallback —
    abstain instead until something has actually scored."""
    from decimal import Decimal

    from endure.base.validator import BaseValidatorNeuron
    from neurons.validator import Validator

    mock_validator_config.neuron.axon_off = True
    mock_validator_config.neuron.disable_set_weights = True
    validator = Validator(config=mock_validator_config)

    calls = {"n": 0}
    monkeypatch.setattr(
        BaseValidatorNeuron,
        "set_weights",
        lambda self: calls.__setitem__("n", calls["n"] + 1),
    )

    validator.scores = [Decimal(0) for _ in validator.scores]
    validator.set_weights()
    assert calls["n"] == 0

    if validator.scores:
        validator.scores[0] = Decimal("0.5")
        validator.set_weights()
        assert calls["n"] == 1


def test_apply_weights_maps_hotkeys_to_metagraph_positions(
    mock_validator_config: bt.Config,
) -> None:
    """The money path (review test-gap): blended EMAs land at the right
    uid positions; unscored hotkeys take zero."""
    from types import SimpleNamespace

    from neurons.validator import Validator

    mock_validator_config.neuron.axon_off = True
    mock_validator_config.neuron.disable_set_weights = True
    validator = Validator(config=mock_validator_config)
    validator.metagraph = SimpleNamespace(hotkeys=["hk-a", "hk-b", "hk-c"])

    validator._apply_weights({"hk-b": Decimal("0.7"), "hk-c": Decimal("0.3")})

    assert validator.scores == [Decimal(0), Decimal("0.7"), Decimal("0.3")]


def test_validator_forward_throttles_to_avoid_busy_spin(
    mock_validator_config: bt.Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from neurons.validator import Validator

    mock_validator_config.neuron.axon_off = True
    mock_validator_config.neuron.disable_set_weights = True
    validator = Validator(config=mock_validator_config)

    class _FailingService:
        def tick(
            self,
            *,
            expected_miners: list[str],
            archive_hotkeys: tuple[str, ...] = (),
        ) -> dict[str, Decimal]:
            raise RuntimeError("boom")

    validator._service = _FailingService()

    recorded: list[float] = []

    async def _spy_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _spy_sleep)

    asyncio.run(validator.forward())

    tick_seconds = int(mock_validator_config.endure.tick_seconds)
    assert recorded == [tick_seconds]
    assert tick_seconds > 0


def test_validator_forward_throttles_on_successful_tick(
    mock_validator_config: bt.Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from neurons.validator import Validator

    mock_validator_config.neuron.axon_off = True
    mock_validator_config.neuron.disable_set_weights = True
    validator = Validator(config=mock_validator_config)

    class _OkService:
        def tick(
            self,
            *,
            expected_miners: list[str],
            archive_hotkeys: tuple[str, ...] = (),
        ) -> dict[str, Decimal] | None:
            return None

    validator._service = _OkService()

    recorded: list[float] = []

    async def _spy_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _spy_sleep)

    asyncio.run(validator.forward())

    tick_seconds = int(mock_validator_config.endure.tick_seconds)
    assert recorded == [tick_seconds]
