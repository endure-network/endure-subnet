from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch

import bittensor as bt
import pytest

from endure.assessment.schemas.forge_lending import FORGE_LENDING_SCHEMA_ID
from endure.assessment.schemas.subnet_alpha_risk import RISK_SCHEMA_ID


@contextmanager
def _patched_chain() -> Iterator[None]:
    fake_wallet = MagicMock()
    fake_wallet.hotkey.ss58_address = "5Test"
    fake_metagraph = MagicMock()
    fake_metagraph.hotkeys = ["5Test"]
    fake_metagraph.n = 1
    fake_metagraph.axons = []
    fake_metagraph.validator_permit = []
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
        yield


def test_miner_bootstraps_in_mock_mode(
    mock_miner_config: bt.Config,
    trap_external_ip: dict[str, int],
) -> None:
    from neurons.miner import Miner

    miner = Miner(config=mock_miner_config)

    assert miner.wallet is not None
    assert miner.axon is not None
    assert miner.axon.ip == "127.0.0.1"
    assert miner.axon.external_ip == "127.0.0.1"
    assert trap_external_ip["count"] == 0


def test_miner_rejects_lending_selection_until_serving_gate(
    mock_miner_config: bt.Config,
) -> None:
    from neurons.miner import Miner

    mock_miner_config.endure.active_schema = FORGE_LENDING_SCHEMA_ID

    with pytest.raises(RuntimeError, match="registered/selectable"):
        Miner(config=mock_miner_config)


def test_miner_wires_served_risk_schema_with_devnet_compression(
    mock_miner_config: bt.Config,
) -> None:
    from neurons.miner import Miner

    mock_miner_config.endure.active_schema = RISK_SCHEMA_ID
    mock_miner_config.endure.devnet_time_compression = True

    miner = Miner(config=mock_miner_config)

    assert miner._schema_id == RISK_SCHEMA_ID


def test_miner_refuses_served_risk_schema_on_finney(
    production_miner_config: bt.Config,
) -> None:
    from neurons.miner import Miner

    production_miner_config.endure.active_schema = RISK_SCHEMA_ID
    production_miner_config.subtensor.chain_endpoint = (
        "wss://entrypoint-finney.opentensor.ai:443"
    )
    production_miner_config.subtensor.network = "finney"

    with _patched_chain(), pytest.raises(RuntimeError, match="R7 soak gate"):
        Miner(config=production_miner_config)


class _FakeAxon:
    def __init__(self, hotkey: str, ip: str = "203.0.113.1", port: int = 8091) -> None:
        self.hotkey = hotkey
        self.ip = ip
        self.port = port
        self.is_serving = True


class _FakeMetagraph:
    n = 3
    hotkeys = ["hk-v1", "hk-v2", "hk-self"]
    axons = [_FakeAxon("hk-v1"), _FakeAxon("hk-v2"), _FakeAxon("hk-self")]
    validator_permit = [True, True, True]


async def test_send_counts_acceptances_and_skips_acked_validators(
    mock_miner_config: bt.Config,
) -> None:
    """The push seam reports how many validators hold the submission, and
    retries only target validators that have not yet accepted — re-pushing
    to an accepting validator would burn its per-round commit rate limit."""
    from endure.protocol.synapses import SubmitCommit
    from endure.protocol.version_contract import CURRENT_VERSION_KEY
    from neurons.miner import Miner

    miner = Miner(config=mock_miner_config)
    miner.metagraph = _FakeMetagraph()
    miner.uid = 2  # hk-self

    pushed: list[list[str]] = []
    accepting = {"hk-v1"}

    async def fake_dendrite(*, axons, synapse, timeout, deserialize):  # noqa: ANN001, ANN202
        pushed.append([axon.hotkey for axon in axons])
        responses = []
        for axon in axons:
            response = synapse.model_copy()
            response.accepted = axon.hotkey in accepting
            responses.append(response)
        return responses

    miner.dendrite = fake_dendrite

    synapse = SubmitCommit(
        round_id="2023-03-06",
        schema_id=RISK_SCHEMA_ID,
        spec_version=CURRENT_VERSION_KEY,
        bundle_hash="ab" * 32,
    )
    first = await miner._send(synapse)
    assert first == 1
    assert pushed[0] == ["hk-v1", "hk-v2"]  # self excluded

    accepting.add("hk-v2")
    second = await miner._send(synapse.model_copy())
    assert second == 2
    assert pushed[1] == ["hk-v2"]  # hk-v1 already accepted — not re-pushed


async def test_send_applies_validator_axon_overrides(
    mock_miner_config: bt.Config,
) -> None:
    from endure.protocol.synapses import SubmitCommit
    from endure.protocol.version_contract import CURRENT_VERSION_KEY
    from neurons.miner import Miner

    mock_miner_config.endure.validator_axon_overrides = "hk-v1=validator:8091"
    miner = Miner(config=mock_miner_config)
    miner.metagraph = _FakeMetagraph()
    miner.uid = 2  # hk-self

    pushed: list[tuple[str, str, int]] = []

    async def fake_dendrite(*, axons, synapse, timeout, deserialize):  # noqa: ANN001, ANN202
        pushed.extend((axon.hotkey, axon.ip, axon.port) for axon in axons)
        responses = []
        for axon in axons:
            response = synapse.model_copy()
            response.accepted = axon.hotkey == "hk-v1"
            responses.append(response)
        return responses

    miner.dendrite = fake_dendrite

    await miner._send(
        SubmitCommit(
            round_id="2023-03-06",
            schema_id=RISK_SCHEMA_ID,
            spec_version=CURRENT_VERSION_KEY,
            bundle_hash="ab" * 32,
        )
    )

    assert pushed == [
        ("hk-v1", "validator", 8091),
        ("hk-v2", "203.0.113.1", 8091),
    ]


@pytest.mark.parametrize(
    "raw",
    (
        "hk-v1=2001:db8::1:8091",
        "hk-v1=validator:8091,hk-v1=other-validator:8092",
    ),
)
def test_validator_axon_overrides_reject_ambiguous_endpoints(raw: str) -> None:
    from neurons.miner import _parse_validator_axon_overrides

    with pytest.raises(ValueError, match="invalid validator axon override"):
        _parse_validator_axon_overrides(raw)


def test_validator_axon_overrides_normalize_hosts() -> None:
    from neurons.miner import _parse_validator_axon_overrides

    assert _parse_validator_axon_overrides(
        "hk-v1= validator :8091,hk-v2=[::1]:8092"
    ) == {
        "hk-v1": ("validator", 8091),
        "hk-v2": ("[::1]", 8092),
    }


def test_main_redacts_startup_endpoint_credentials() -> None:
    from neurons.miner import main

    credential_url = "".join(
        ("wss://user:password", "@rpc.example.invalid/ws?token=secret")
    )
    error_log = MagicMock()
    with (
        patch("neurons.miner.Miner", side_effect=RuntimeError(credential_url)),
        patch("neurons.miner.bt.logging.error", error_log),
        pytest.raises(SystemExit) as exit_info,
    ):
        main()

    assert exit_info.value.code == 1
    rendered = str(error_log.call_args.args[0])
    assert "password" not in rendered
    assert "secret" not in rendered
    assert "<redacted-endpoint>" in rendered


def test_main_exits_nonzero_and_cleans_up_on_worker_failure() -> None:
    from neurons.miner import main

    miner = MagicMock()
    miner.thread.is_alive.return_value = False
    context = MagicMock()
    context.__enter__.return_value = miner

    with (
        patch("neurons.miner.Miner", return_value=context),
        pytest.raises(SystemExit) as exit_info,
    ):
        main()

    assert exit_info.value.code == 1
    context.__exit__.assert_called_once()
