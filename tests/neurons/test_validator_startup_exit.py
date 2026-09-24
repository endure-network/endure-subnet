import subprocess
import sys
import threading
import time
from functools import partial
from pathlib import Path
from typing import Never
from unittest.mock import MagicMock, patch

import pytest

from endure.assessment.schemas.subnet_alpha_risk import RISK_SCHEMA_ID
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
        patch.object(
            validator, "_WATCHDOG_TEARDOWN_GRACE_SECONDS", _TEST_GRACE_SECONDS
        ),
        patch.object(validator.bt.logging, "error", side_effect=print),
        patch("bittensor.Subtensor", side_effect=blocked_subtensor),
        patch("endure.live.alpha_market_data.BittensorSubnetInfoFetcher", make_fetcher),
        patch(
            "endure.live.alpha_market_data.LiveAlphaPriceProviderConfig",
            partial(LiveAlphaPriceProviderConfig, max_attempts=1),
        ),
    ):
        try:
            validator.main()
        except SystemExit as error:
            # Reaching SystemExit alone is insufficient: CPython still joins the
            # abandoned executor thread. The parent must observe actual exit.
            print(f"startup exit requested: {error.code}", flush=True)
            raise


def _run_clean_shutdown() -> None:
    from neurons import validator

    stop = threading.Event()
    stop.set()
    context = MagicMock()
    with (
        patch.object(validator, "Validator", return_value=context),
        patch.object(validator, "configure_log_shipping"),
        patch.object(validator, "install_shutdown_handlers", return_value=stop),
        patch.object(
            validator, "_WATCHDOG_TEARDOWN_GRACE_SECONDS", _TEST_GRACE_SECONDS
        ),
    ):
        validator.main()
        # A mistakenly armed timer would kill this otherwise healthy process.
        time.sleep(_TEST_GRACE_SECONDS * 3)
    assert context.__exit__.call_count == 1
    print("graceful shutdown completed", flush=True)


@pytest.mark.parametrize("scenario", ["blocked_archive", "clean_shutdown"])
def test_main_process_exit_is_bounded_and_preserves_clean_shutdown(
    tmp_path: Path, scenario: str
) -> None:
    child = (
        "import sys; "
        "from tests.neurons.test_validator_startup_exit import "
        "_run_blocked_archive_startup; "
        "_run_blocked_archive_startup(sys.argv[1])"
        if scenario == "blocked_archive"
        else "from tests.neurons.test_validator_startup_exit import "
        "_run_clean_shutdown; _run_clean_shutdown()"
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

    assert "archive-password" not in output
    assert "private-key" not in output
    assert "secret" not in output
    if scenario == "blocked_archive":
        assert process.returncode == 1, output
        assert "archive worker entered; daemon=False" in output
        assert "validator failed: AlphaMarketDataUnavailable:" in output
        assert "startup exit requested: 1" in output
    else:
        assert process.returncode == 0, output
        assert "graceful shutdown completed" in output
