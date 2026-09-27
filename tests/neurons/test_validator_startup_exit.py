"""Every neuron exit path terminates, including hangs in interpreter finalization."""

import select
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Never
from unittest.mock import MagicMock, patch

import pytest

from endure.assessment.schemas.subnet_alpha_risk import RISK_SCHEMA_ID
from endure.base.shutdown import run_entrypoint
from endure.live.alpha_market_data import (
    BittensorSubnetInfoFetcher,
    LiveAlphaPriceProviderConfig,
)
from endure.protocol.consensus_policy import MAINNET_GENESIS_HASH
from tests.conftest import _base_config
from tests.scoring.test_live_market_data import (
    FakeArchiveSubstrate,
    _archive_probe_fetcher,
)

_ENDPOINT = "wss://archive-user:archive-password@example.org/private-key?token=secret"
_CHILD_TIMEOUT_SECONDS = 15
_TEST_GRACE_SECONDS = 0.2


class _StuckOnFinalize:
    """Stands in for a SyncSubstrate whose ``__del__`` joins a dead websocket."""

    def __del__(self) -> None:
        threading.Event().wait()


def _leave_unclosed_sdk_client() -> None:
    # A module global is only released during interpreter finalization, after
    # daemon threads (and so any forced-exit timer) can no longer run.
    import __main__

    vars(__main__)["unclosed_sdk_client"] = _StuckOnFinalize()


def _entrypoint(main: Callable[[], None]) -> Never:
    run_entrypoint(main, grace_seconds=_TEST_GRACE_SECONDS)


def _run_blocked_archive_startup(tmp_dir: str) -> None:
    from neurons import validator

    config = _base_config(Path(tmp_dir), ["validator"], runtime_mode="live")
    config.netuid = 30
    config.subtensor.network = "finney"
    config.subtensor.chain_endpoint = ""
    config.endure.active_schema = RISK_SCHEMA_ID
    config.endure.serving_stage = "mainnet"
    config.endure.market_data_endpoint = _ENDPOINT
    source = _archive_probe_fetcher()
    substrate = FakeArchiveSubstrate(
        finalized=source.finalized,
        timestamps_by_block=source.timestamps_by_block,
        genesis=MAINNET_GENESIS_HASH,
    )

    def blocked_subtensor(*, network: str, archive_endpoints: list[str]) -> Never:
        assert network == _ENDPOINT
        assert archive_endpoints == [_ENDPOINT]
        worker = threading.current_thread()
        assert worker.name.startswith("alpha-archive-timeout")
        print(f"archive worker entered; daemon={worker.daemon}", flush=True)
        forever = threading.Event()
        while True:
            forever.wait()

    def make_fetcher(endpoint: str) -> BittensorSubnetInfoFetcher:
        fetcher = BittensorSubnetInfoFetcher(
            endpoint,
            request_timeout_seconds=0.05,
            min_request_interval_seconds=0.0,
        )
        fetcher._make_substrate = lambda: substrate
        return fetcher

    with (
        patch.object(validator.Validator, "build_config", return_value=config),
        patch.object(validator, "configure_log_shipping"),
        patch.object(validator, "_require_hotkey"),
        patch.object(validator.bt.logging, "error", side_effect=print),
        patch(
            "endure.live.alpha_market_data."
            "LIVE_MARKET_DATA_ARCHIVE_PROBE_TIMEOUT_SECONDS",
            1.0,
        ),
        patch("bittensor.Subtensor", side_effect=blocked_subtensor),
        patch("endure.live.alpha_market_data.BittensorSubnetInfoFetcher", make_fetcher),
        patch(
            "endure.live.alpha_market_data.LiveAlphaPriceProviderConfig",
            partial(LiveAlphaPriceProviderConfig, max_attempts=1),
        ),
    ):
        _entrypoint(validator.main)


def _run_validator_unregistered_exit() -> None:
    from neurons import validator

    def unregistered_validator() -> Never:
        # BaseNeuron.check_registered() calls sys.exit(1) with the SDK
        # subtensor still open; its __del__ runs during finalization.
        _leave_unclosed_sdk_client()
        print("hotkey not registered; exiting", flush=True)
        sys.exit(1)

    with (
        patch.object(validator, "Validator", side_effect=unregistered_validator),
        patch.object(validator, "configure_log_shipping"),
    ):
        _entrypoint(validator.main)


def _run_validator_dev_only_refusal() -> None:
    from neurons import validator

    def refused_validator() -> Never:
        _leave_unclosed_sdk_client()
        raise validator.DevOnlyConfigError("compression refused on mainnet")

    with (
        patch.object(validator, "Validator", side_effect=refused_validator),
        patch.object(validator, "configure_log_shipping"),
        patch.object(validator.bt.logging, "error", side_effect=print),
    ):
        _entrypoint(validator.main)


def _run_miner_unregistered_exit() -> None:
    from neurons import miner

    def unregistered_miner() -> Never:
        _leave_unclosed_sdk_client()
        print("hotkey not registered; exiting", flush=True)
        sys.exit(1)

    with (
        patch.object(miner, "Miner", side_effect=unregistered_miner),
        patch.object(miner, "configure_log_shipping"),
    ):
        _entrypoint(miner.main)


def _watchdog_context() -> MagicMock:
    context = MagicMock()
    context.chain_rpc_restart_required.return_value = False
    context.watchdog_exit_reason.return_value = "validator loop thread exited"
    return context


def _run_validator_watchdog_exit() -> None:
    from neurons import validator

    _leave_unclosed_sdk_client()
    with (
        patch.object(validator, "Validator", return_value=_watchdog_context()),
        patch.object(validator, "configure_log_shipping"),
        patch.object(
            validator, "install_shutdown_handlers", return_value=threading.Event()
        ),
        patch.object(validator.bt.logging, "error", side_effect=print),
    ):
        _entrypoint(validator.main)


def _run_validator_clean_shutdown() -> None:
    from neurons import validator

    _leave_unclosed_sdk_client()
    stop = threading.Event()
    stop.set()
    context = MagicMock()
    context.chain_rpc_restart_required.return_value = False
    context.__exit__.side_effect = lambda *_: print("teardown ran", flush=True)
    with (
        patch.object(validator, "Validator", return_value=context),
        patch.object(validator, "configure_log_shipping"),
        patch.object(validator, "install_shutdown_handlers", return_value=stop),
    ):
        _entrypoint(validator.main)


_CHILDREN = {
    "blocked_archive": ("_run_blocked_archive_startup(sys.argv[1])", 1),
    "validator_unregistered": ("_run_validator_unregistered_exit()", 1),
    "validator_dev_only_refusal": ("_run_validator_dev_only_refusal()", 1),
    "miner_unregistered": ("_run_miner_unregistered_exit()", 1),
    "validator_watchdog": ("_run_validator_watchdog_exit()", 1),
    "validator_clean_shutdown": ("_run_validator_clean_shutdown()", 0),
}
_MARKERS = {
    "blocked_archive": (
        "archive worker entered; daemon=False",
        "validator failed: AlphaMarketDataUnavailable:",
    ),
    "validator_unregistered": ("hotkey not registered; exiting",),
    "validator_dev_only_refusal": ("validator refused to start:",),
    "miner_unregistered": ("hotkey not registered; exiting",),
    "validator_watchdog": ("validator watchdog exiting: validator loop thread",),
    "validator_clean_shutdown": ("teardown ran",),
}


@pytest.mark.parametrize("scenario", sorted(_CHILDREN))
def test_process_exits_without_waiting_on_workers_or_finalization(
    tmp_path: Path, scenario: str
) -> None:
    call, expected_code = _CHILDREN[scenario]
    child = (
        "import sys; "
        f"from tests.neurons.test_validator_startup_exit import {call.split('(')[0]}; "
        f"{call}"
    )
    with subprocess.Popen(
        [sys.executable, "-u", "-c", child, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ) as process:
        try:
            output, _ = process.communicate(timeout=_CHILD_TIMEOUT_SECONDS)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    assert process.returncode == expected_code, output
    for marker in _MARKERS[scenario]:
        assert marker in output, output
    assert "archive-password" not in output
    assert "private-key" not in output
    assert "secret" not in output


def _run_wedged_construction(neuron: str) -> None:
    import importlib

    module = importlib.import_module(f"neurons.{neuron}")

    def wedged(*_args: object, **_kwargs: object) -> Never:
        # A connect or metagraph fetch that never returns: only the stop event
        # set by the signal handler can notice the shutdown request.
        _leave_unclosed_sdk_client()
        print("construction wedged", flush=True)
        forever = threading.Event()
        while True:
            forever.wait()

    with (
        patch.object(module, "Validator" if neuron == "validator" else "Miner", wedged),
        patch.object(module, "configure_log_shipping"),
        patch.object(module, "_STARTUP_SHUTDOWN_GRACE_SECONDS", _TEST_GRACE_SECONDS),
    ):
        _entrypoint(module.main)


@pytest.mark.parametrize("neuron", ["validator", "miner"])
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_shutdown_signal_during_wedged_construction_exits(
    neuron: str, signum: signal.Signals
) -> None:
    child = (
        "from tests.neurons.test_validator_startup_exit import "
        f"_run_wedged_construction; _run_wedged_construction({neuron!r})"
    )
    with subprocess.Popen(
        [sys.executable, "-u", "-c", child],
        cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ) as process:
        assert process.stdout is not None
        try:
            seen: list[str] = []
            deadline = time.monotonic() + _CHILD_TIMEOUT_SECONDS
            while "construction wedged\n" not in seen:
                ready, _, _ = select.select([process.stdout], [], [], 1.0)
                assert time.monotonic() < deadline, "".join(seen)
                if ready:
                    line = process.stdout.readline()
                    assert line, "".join(seen)
                    seen.append(line)
            process.send_signal(signum)
            output, _ = process.communicate(timeout=_CHILD_TIMEOUT_SECONDS)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    assert process.returncode == 1, "".join(seen) + output
    assert "shutdown requested during startup" in output


def _run_wedged_real_construction(tmp_dir: str) -> None:
    from endure.runtime.live import LiveRuntimeProvider
    from neurons import validator

    config = _base_config(Path(tmp_dir), ["validator"], runtime_mode="live")
    config.netuid = 30
    config.subtensor.network = "test"
    config.subtensor.chain_endpoint = ""
    config.endure.active_schema = RISK_SCHEMA_ID
    config.endure.serving_stage = "testnet"

    def wedged_create_base(_self: object, _config: object) -> Never:
        # The real Validator() construction path reaches the SDK connect and
        # never returns; only the startup guard can notice SIGTERM/SIGINT.
        _leave_unclosed_sdk_client()
        print("construction wedged", flush=True)
        forever = threading.Event()
        while True:
            forever.wait()

    with (
        patch.object(validator.Validator, "build_config", return_value=config),
        patch.object(validator, "configure_log_shipping"),
        patch.object(LiveRuntimeProvider, "create_base", wedged_create_base),
        patch.object(validator, "_STARTUP_SHUTDOWN_GRACE_SECONDS", 0.5),
    ):
        _entrypoint(validator.main)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_signal_while_real_construction_wedges_in_create_base_exits(
    tmp_path: Path, signum: signal.Signals
) -> None:
    child = (
        "import sys; from tests.neurons.test_validator_startup_exit import "
        "_run_wedged_real_construction; _run_wedged_real_construction(sys.argv[1])"
    )
    with subprocess.Popen(
        [sys.executable, "-u", "-c", child, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ) as process:
        assert process.stdout is not None
        try:
            seen: list[str] = []
            deadline = time.monotonic() + 60
            while "construction wedged\n" not in seen:
                ready, _, _ = select.select([process.stdout], [], [], 1.0)
                assert time.monotonic() < deadline, "".join(seen)
                if ready:
                    line = process.stdout.readline()
                    assert line, "".join(seen)
                    seen.append(line)
            process.send_signal(signum)
            output, _ = process.communicate(timeout=_CHILD_TIMEOUT_SECONDS)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    assert process.returncode == 1, "".join(seen) + output
    assert "shutdown requested during startup" in output
