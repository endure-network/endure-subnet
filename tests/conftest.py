"""Shared pytest fixtures for the Endure test suite.

Fixtures exported:
- isolate_home: autouse, reroutes HOME to a tmp dir so bittensor wallet
  creation never touches the real user profile.
- reset_mock_subtensor_state: autouse, clears the bittensor module-global
  mock chain_state between tests so subnet/registration state does not
  leak (see tests/test_mock.py for background).
- mock_runtime_provider / live_runtime_provider: explicit runtime-provider
  fixtures used by base-class and production-path tests.
- mock_wallet: fresh bt.Wallet (mock backend) per test.
- mock_config_base: bt.Config populated with all BaseNeuron args plus
  --netuid 1, a tmp_path logging_dir, and --neuron.dont_save_events.
- mock_miner_config: mock_config_base + miner-specific args.
- mock_validator_config: mock_config_base + validator-specific args.
- production_miner_config / production_validator_config: same args
  with runtime.mode=live, so tests can exercise the bt.Wallet/bt.Subtensor
  production branch under monkeypatched chain primitives.
- migrated_storage: a migrated, empty Storage on a tmp SQLite DB.
- storage: the default empty migrated_storage; modules that need seeded
  rounds override this fixture locally (depending on migrated_storage).
"""

from __future__ import annotations

import argparse
import tempfile
from collections.abc import Iterator
from pathlib import Path

import bittensor as bt
import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from bittensor.utils.mock.subtensor_mock import __GLOBAL_MOCK_STATE__
from bittensor_wallet.mock import get_mock_wallet

from endure.runtime.live import LiveRuntimeProvider
from endure.runtime.mock import MockRuntimeProvider
from endure.storage.repository import Storage
from endure.utils.config import add_args, add_miner_args, add_validator_args


@pytest.fixture(autouse=True)
def isolate_home(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with tempfile.TemporaryDirectory(prefix="endure-test-") as home:
        monkeypatch.setenv("HOME", home)
        yield


@pytest.fixture(autouse=True)
def reset_mock_subtensor_state() -> None:
    __GLOBAL_MOCK_STATE__.clear()


@pytest.fixture
def mock_wallet() -> bt.Wallet:
    return get_mock_wallet()


@pytest.fixture
def mock_runtime_provider() -> MockRuntimeProvider:
    return MockRuntimeProvider()


@pytest.fixture
def live_runtime_provider() -> LiveRuntimeProvider:
    return LiveRuntimeProvider()


@pytest.fixture
def trap_external_ip(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"count": 0}

    def _fake_get_external_ip() -> str:
        calls["count"] += 1
        return "127.0.0.1"

    monkeypatch.setattr(
        "bittensor.utils.networking.get_external_ip",
        _fake_get_external_ip,
    )
    return calls


def _base_config(
    tmp_path: Path,
    extra_args: list[str],
    *,
    runtime_mode: str | None = None,
) -> bt.Config:
    parser = argparse.ArgumentParser()
    bt.Wallet.add_args(parser)
    bt.Subtensor.add_args(parser)
    bt.logging.add_args(parser)
    bt.Axon.add_args(parser)
    add_args(None, parser)
    for fn in extra_args_to_add_fns(extra_args):
        fn(None, parser)
    cli_args = [
        "--netuid",
        "1",
        "--neuron.dont_save_events",
        "--wallet.name",
        "test-cold",
        "--wallet.hotkey",
        "test-hot",
        "--axon.port",
        "9091",
    ]
    if runtime_mode is not None:
        cli_args.extend(["--runtime.mode", runtime_mode])
    cfg = bt.Config(parser, args=cli_args)
    cfg.logging.logging_dir = str(tmp_path)
    # Keep test databases inside the test sandbox, never the repo var/.
    cfg.endure.database_url = f"sqlite:///{tmp_path}/endure-test.db"
    return cfg


def extra_args_to_add_fns(keys: list[str]):
    """Map fixture role labels to the argparse adder functions."""
    mapping = {
        "miner": add_miner_args,
        "validator": add_validator_args,
    }
    return [mapping[k] for k in keys]


@pytest.fixture
def mock_config_base(tmp_path: Path) -> bt.Config:
    return _base_config(tmp_path, [])


@pytest.fixture
def mock_miner_config(tmp_path: Path) -> bt.Config:
    return _base_config(tmp_path, ["miner"], runtime_mode="mock")


@pytest.fixture
def mock_validator_config(tmp_path: Path) -> bt.Config:
    return _base_config(tmp_path, ["validator"], runtime_mode="mock")


@pytest.fixture
def production_miner_config(tmp_path: Path) -> bt.Config:
    """Non-mock miner config — exercises the bt.Wallet/bt.Subtensor branch."""
    return _base_config(tmp_path, ["miner"], runtime_mode="live")


@pytest.fixture
def production_validator_config(tmp_path: Path) -> bt.Config:
    """Non-mock validator config — exercises the bt.Wallet/bt.Subtensor branch."""
    return _base_config(tmp_path, ["validator"], runtime_mode="live")


@pytest.fixture
def migrated_storage(tmp_path: Path) -> Storage:
    """A migrated, empty Storage on a throwaway SQLite DB.

    Single-sources the alembic-upgrade + ``Storage.from_url`` bootstrap that
    the domain test modules had copied verbatim. The migration-machinery tests
    (test_migrations / test_tables) keep their own explicit bootstrap, since
    the migration is exactly what they exercise.
    """
    repo_root = Path(__file__).resolve().parents[1]
    url = f"sqlite:///{tmp_path / 'endure.db'}"
    config = AlembicConfig(str(repo_root / "alembic.ini"))
    config.set_main_option(
        "script_location", str(repo_root / "endure/storage/migrations")
    )
    config.set_main_option("sqlalchemy.url", url)
    alembic_command.upgrade(config, "head")
    return Storage.from_url(url)


@pytest.fixture
def storage(migrated_storage: Storage) -> Storage:
    """Empty migrated storage. Modules that seed rounds override this."""
    return migrated_storage
