import threading
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from decimal import Decimal
from unittest.mock import MagicMock, patch

import bittensor as bt
import numpy as np
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


def test_main_hard_exits_when_rpc_abandonment_capacity_is_reached() -> None:
    from neurons.miner import main

    miner = MagicMock()
    miner.chain_rpc_restart_required.return_value = True
    context = MagicMock()
    context.__enter__.return_value = miner

    with (
        patch(
            "neurons.miner.install_shutdown_handlers",
            return_value=threading.Event(),
        ),
        patch("neurons.miner.Miner", return_value=context),
        patch("neurons.miner.os._exit", side_effect=SystemExit(1)) as hard_exit,
        pytest.raises(SystemExit) as exit_info,
    ):
        main()

    assert exit_info.value.code == 1
    hard_exit.assert_called_once_with(1)


def test_main_hard_exits_when_watchdog_races_rpc_abandonment() -> None:
    from neurons.miner import main

    miner = MagicMock()
    # Given: the worker latches and dies between the latch check and the
    # liveness probe.
    miner.chain_rpc_restart_required.side_effect = [False, True]
    miner.thread = None
    context = MagicMock()
    context.__enter__.return_value = miner

    with (
        patch(
            "neurons.miner.install_shutdown_handlers",
            return_value=threading.Event(),
        ),
        patch("neurons.miner.Miner", return_value=context),
        patch("neurons.miner.os._exit", side_effect=SystemExit(1)) as hard_exit,
        pytest.raises(SystemExit) as exit_info,
    ):
        main()

    # Then: the watchdog path still restarts hard instead of exiting normally.
    assert exit_info.value.code == 1
    hard_exit.assert_called_once_with(1)


def test_main_hard_exits_when_shutdown_signal_races_rpc_abandonment() -> None:
    from neurons.miner import main

    miner = MagicMock()
    miner.chain_rpc_restart_required.return_value = True
    context = MagicMock()
    context.__enter__.return_value = miner
    # Given: the shutdown signal already arrived when the latch is checked.
    already_stopped = threading.Event()
    already_stopped.set()

    with (
        patch(
            "neurons.miner.install_shutdown_handlers",
            return_value=already_stopped,
        ),
        patch("neurons.miner.Miner", return_value=context),
        patch("neurons.miner.os._exit", side_effect=SystemExit(1)) as hard_exit,
        pytest.raises(SystemExit) as exit_info,
    ):
        main()

    # Then: the process still restarts hard instead of exiting normally.
    assert exit_info.value.code == 1
    hard_exit.assert_called_once_with(1)


def test_main_hard_exits_when_rpc_abandonment_races_lifecycle_teardown() -> None:
    from neurons.miner import main

    miner = MagicMock()
    latched = {"value": False}
    miner.chain_rpc_restart_required.side_effect = lambda: latched["value"]
    context = MagicMock()
    context.__enter__.return_value = miner
    # Given: the worker latches only while __exit__ joins it, after every
    # in-body recheck has already passed.
    already_stopped = threading.Event()
    already_stopped.set()

    def _latch_during_teardown(*_args: object) -> None:
        latched["value"] = True

    context.__exit__.side_effect = _latch_during_teardown

    with (
        patch(
            "neurons.miner.install_shutdown_handlers",
            return_value=already_stopped,
        ),
        patch("neurons.miner.Miner", return_value=context),
        patch("neurons.miner.os._exit", side_effect=SystemExit(1)) as hard_exit,
        pytest.raises(SystemExit) as exit_info,
    ):
        main()

    # Then: the post-teardown recheck still restarts hard.
    assert exit_info.value.code == 1
    hard_exit.assert_called_once_with(1)


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


def test_miner_warns_when_validator_stake_weight_gate_is_live(
    production_miner_config: bt.Config,
) -> None:
    from neurons.miner import Miner

    production_miner_config.subtensor.network = "test"
    production_miner_config.endure.serving_stage = "testnet"
    production_miner_config.endure.min_validator_stake_weight = Decimal("1000")
    warning = MagicMock()

    with _patched_chain(), patch("neurons.miner.bt.logging.warning", warning):
        Miner(config=production_miner_config)

    assert any(
        "min_validator_stake_weight" in str(call.args[0])
        for call in warning.call_args_list
    )


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
    S = np.array([423000, 51000, 1], dtype=np.float32)


class _IterationSensitiveStakeWeights(np.ndarray):
    def __iter__(self) -> Iterator[np.float32]:
        raise AssertionError(
            "stake weights must be normalized before scalar conversion"
        )


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


async def test_send_skips_peers_below_the_validator_stake_floor(
    mock_miner_config: bt.Config,
) -> None:
    """On testnet every registered neuron holds validator_permit, so the
    permit alone would make miners push to each other; the stake floor
    keeps low-stake permit holders out of the target set."""
    from endure.protocol.synapses import SubmitCommit
    from endure.protocol.version_contract import CURRENT_VERSION_KEY
    from neurons.miner import Miner

    mock_miner_config.endure.min_validator_stake_weight = Decimal("1000")
    miner = Miner(config=mock_miner_config)
    metagraph = _FakeMetagraph()
    metagraph.S = np.array(
        [423000, 1, 1], dtype=np.float32
    )  # hk-v2 is a permit-holding miner
    miner.metagraph = metagraph
    miner.uid = 2  # hk-self

    pushed: list[list[str]] = []

    async def fake_dendrite(*, axons, synapse, timeout, deserialize):  # noqa: ANN001, ANN202
        pushed.append([axon.hotkey for axon in axons])
        responses = []
        for _ in axons:
            response = synapse.model_copy()
            response.accepted = True
            responses.append(response)
        return responses

    miner.dendrite = fake_dendrite

    total = await miner._send(
        SubmitCommit(
            round_id="2023-03-06",
            schema_id=RISK_SCHEMA_ID,
            spec_version=CURRENT_VERSION_KEY,
            bundle_hash="ab" * 32,
        )
    )

    assert total == 1
    assert pushed == [["hk-v1"]]


def test_snapshot_push_targets_normalizes_stake_weights_before_conversion(
    mock_miner_config: bt.Config,
) -> None:
    from neurons.miner import Miner

    mock_miner_config.endure.min_validator_stake_weight = Decimal("1000")
    miner = Miner(config=mock_miner_config)
    metagraph = _FakeMetagraph()
    metagraph.S = np.array([423000, 1, 1], dtype=np.float32).view(
        _IterationSensitiveStakeWeights
    )
    miner.metagraph = metagraph
    miner.uid = 2

    targets = miner._snapshot_push_targets(set(), set())

    assert [hotkey for hotkey, _ in targets] == ["hk-v1"]


async def test_send_zero_stake_floor_disables_the_gate(
    mock_miner_config: bt.Config,
) -> None:
    from endure.protocol.synapses import SubmitCommit
    from endure.protocol.version_contract import CURRENT_VERSION_KEY
    from neurons.miner import Miner

    mock_miner_config.endure.min_validator_stake_weight = Decimal("0")
    miner = Miner(config=mock_miner_config)
    metagraph = _FakeMetagraph()
    metagraph.S = np.array([1, 1, 1], dtype=np.float32)
    miner.metagraph = metagraph
    miner.uid = 2  # hk-self

    pushed: list[list[str]] = []

    async def fake_dendrite(*, axons, synapse, timeout, deserialize):  # noqa: ANN001, ANN202
        pushed.append([axon.hotkey for axon in axons])
        return [synapse.model_copy() for _ in axons]

    miner.dendrite = fake_dendrite

    await miner._send(
        SubmitCommit(
            round_id="2023-03-06",
            schema_id=RISK_SCHEMA_ID,
            spec_version=CURRENT_VERSION_KEY,
            bundle_hash="ab" * 32,
        )
    )

    assert pushed == [["hk-v1", "hk-v2"]]


async def test_send_stops_retrying_canonical_unknown_synapse_rejections(
    mock_miner_config: bt.Config,
) -> None:
    """A peer whose axon answers 404 (bittensor's UnknownSynapseError) lacks
    the Endure handlers entirely; retrying it within the round only produces
    an error storm, so it is excluded for the rest of the round."""
    from endure.protocol.synapses import SubmitCommit
    from endure.protocol.version_contract import CURRENT_VERSION_KEY
    from neurons.miner import Miner

    miner = Miner(config=mock_miner_config)
    miner.metagraph = _FakeMetagraph()
    miner.uid = 2  # hk-self

    pushed: list[list[str]] = []

    async def fake_dendrite(*, axons, synapse, timeout, deserialize):  # noqa: ANN001, ANN202
        pushed.append([axon.hotkey for axon in axons])
        responses = []
        for axon in axons:
            response = synapse.model_copy()
            response.accepted = False
            if axon.hotkey == "hk-v2":
                response.dendrite = bt.TerminalInfo(
                    status_code=404,
                    status_message=(
                        f"Synapse name '{response.name}' not found. "
                        "Available synapses ['Synapse']"
                    ),
                )
            responses.append(response)
        return responses

    miner.dendrite = fake_dendrite

    synapse = SubmitCommit(
        round_id="2023-03-06",
        schema_id=RISK_SCHEMA_ID,
        spec_version=CURRENT_VERSION_KEY,
        bundle_hash="ab" * 32,
    )
    await miner._send(synapse)
    assert pushed[0] == ["hk-v1", "hk-v2"]

    await miner._send(synapse.model_copy())
    assert pushed[1] == ["hk-v1"]  # hk-v2 excluded for the round, still retried v1

    other_round = synapse.model_copy()
    other_round.round_id = "2023-03-07"
    await miner._send(other_round)
    assert pushed[2] == ["hk-v1", "hk-v2"]  # exclusion is per-round


async def test_send_logs_the_rejection_reason_for_refused_pushes(
    mock_miner_config: bt.Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A validator that refuses a push (e.g. a stake-floor blacklist) must
    surface its reason in the miner log — a silently unacked push leaves the
    operator with no way to see why no commit is ever accepted."""
    from endure.protocol.synapses import SubmitCommit
    from endure.protocol.version_contract import CURRENT_VERSION_KEY
    from neurons.miner import Miner

    miner = Miner(config=mock_miner_config)
    miner.metagraph = _FakeMetagraph()
    miner.uid = 2  # hk-self

    async def fake_dendrite(*, axons, synapse, timeout, deserialize):  # noqa: ANN001, ANN202
        responses = []
        for _ in axons:
            response = synapse.model_copy()
            response.accepted = False
            response.dendrite = bt.TerminalInfo(
                status_code=403,
                status_message="Forbidden. Key is blacklisted: Insufficient stake.",
            )
            responses.append(response)
        return responses

    miner.dendrite = fake_dendrite
    warning_mock = MagicMock()
    monkeypatch.setattr(bt.logging, "warning", warning_mock)

    synapse = SubmitCommit(
        round_id="2023-03-06",
        schema_id=RISK_SCHEMA_ID,
        spec_version=CURRENT_VERSION_KEY,
        bundle_hash="ab" * 32,
    )
    total = await miner._send(synapse)

    assert total == 0
    rendered = "\n".join(str(call.args[0]) for call in warning_mock.call_args_list)
    assert "Insufficient stake" in rendered

    # When: the same validators keep rejecting on later ticks for the same
    # reason, the warning is not repeated.
    first_tick_warnings = warning_mock.call_count
    await miner._send(synapse.model_copy())
    assert warning_mock.call_count == first_tick_warnings


async def test_send_dedups_rotating_reasons_and_labels_transport_failures(
    mock_miner_config: bt.Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from endure.protocol.synapses import SubmitCommit
    from endure.protocol.version_contract import CURRENT_VERSION_KEY
    from neurons.miner import Miner

    miner = Miner(config=mock_miner_config)
    miner.metagraph = _FakeMetagraph()
    miner.uid = 2  # hk-self
    tick = {"count": 0}

    async def fake_dendrite(*, axons, synapse, timeout, deserialize):  # noqa: ANN001, ANN202
        tick["count"] += 1
        responses = []
        for axon in axons:
            response = synapse.model_copy()
            response.accepted = False
            if axon.hotkey == "hk-v1":
                # Given: a policy rejection whose peer-controlled reason text
                # rotates on every tick.
                response.dendrite = bt.TerminalInfo(
                    status_code=403,
                    status_message=f"Forbidden. rotating nonce {tick['count']}",
                )
            else:
                # Given: a dendrite-local transport failure that never carried
                # a validator verdict.
                response.dendrite = bt.TerminalInfo(
                    status_code=503,
                    status_message="Service at 203.0.113.1:8091 unavailable.",
                )
            responses.append(response)
        return responses

    miner.dendrite = fake_dendrite
    warning_mock = MagicMock()
    monkeypatch.setattr(bt.logging, "warning", warning_mock)

    synapse = SubmitCommit(
        round_id="2023-03-06",
        schema_id=RISK_SCHEMA_ID,
        spec_version=CURRENT_VERSION_KEY,
        bundle_hash="ab" * 32,
    )
    await miner._send(synapse)
    await miner._send(synapse.model_copy())
    await miner._send(synapse.model_copy())

    rendered = [str(call.args[0]) for call in warning_mock.call_args_list]
    rejected = [line for line in rendered if "rejected push" in line]
    undelivered = [line for line in rendered if "push undelivered" in line]
    # Then: the rotating reason cannot re-trigger the warning; the first
    # reason is kept.
    assert len(rejected) == 1
    assert "hk-v1" in rejected[0]
    assert "rotating nonce 1" in rejected[0]
    # Then: the transport failure is labeled as undelivered, not as a
    # validator rejection, and is deduplicated the same way.
    assert len(undelivered) == 1
    assert "hk-v2" in undelivered[0]


async def test_unknown_synapse_match_tracks_installed_bittensor_contract() -> None:
    from bittensor.core.axon import AxonMiddleware, log_and_handle_error
    from bittensor.core.errors import UnknownSynapseError

    from neurons.miner import _rejected_as_unknown_synapse

    response = bt.Synapse()
    axon = MagicMock()
    axon.forward_class_types = {"DifferentSynapse": bt.Synapse}
    middleware = AxonMiddleware(MagicMock(), axon)
    request = MagicMock()
    request.url.path = f"/{response.name}"

    with pytest.raises(UnknownSynapseError) as error:
        await middleware.preprocess(request)

    mapped = log_and_handle_error(response, error.value)
    response.dendrite = bt.TerminalInfo(
        status_code=mapped.axon.status_code,
        status_message=mapped.axon.status_message,
    )
    assert _rejected_as_unknown_synapse(response)


async def test_send_retries_generic_http_404_responses(
    mock_miner_config: bt.Config,
) -> None:
    from endure.protocol.synapses import SubmitCommit
    from endure.protocol.version_contract import CURRENT_VERSION_KEY
    from neurons.miner import Miner

    miner = Miner(config=mock_miner_config)
    miner.metagraph = _FakeMetagraph()
    miner.uid = 2
    pushed: list[list[str]] = []

    async def fake_dendrite(*, axons, synapse, timeout, deserialize):  # noqa: ANN001, ANN202
        pushed.append([axon.hotkey for axon in axons])
        responses = []
        for axon in axons:
            response = synapse.model_copy()
            response.accepted = False
            if axon.hotkey == "hk-v2":
                response.dendrite = bt.TerminalInfo(
                    status_code=404,
                    status_message="proxy route not found",
                )
            responses.append(response)
        return responses

    miner.dendrite = fake_dendrite
    synapse = SubmitCommit(
        round_id="2023-03-06",
        schema_id=RISK_SCHEMA_ID,
        spec_version=CURRENT_VERSION_KEY,
        bundle_hash="ab" * 32,
    )

    await miner._send(synapse)
    await miner._send(synapse.model_copy())

    assert pushed == [["hk-v1", "hk-v2"], ["hk-v1", "hk-v2"]]


async def test_send_handles_short_stake_weight_snapshot(
    mock_miner_config: bt.Config,
) -> None:
    from endure.protocol.synapses import SubmitCommit
    from endure.protocol.version_contract import CURRENT_VERSION_KEY
    from neurons.miner import Miner

    mock_miner_config.endure.min_validator_stake_weight = Decimal("1000")
    miner = Miner(config=mock_miner_config)
    metagraph = _FakeMetagraph()
    metagraph.S = np.array([423000], dtype=np.float32)
    miner.metagraph = metagraph
    miner.uid = 2
    pushed: list[list[str]] = []

    async def fake_dendrite(*, axons, synapse, timeout, deserialize):  # noqa: ANN001, ANN202
        pushed.append([axon.hotkey for axon in axons])
        return [synapse.model_copy() for _ in axons]

    miner.dendrite = fake_dendrite

    await miner._send(
        SubmitCommit(
            round_id="2023-03-06",
            schema_id=RISK_SCHEMA_ID,
            spec_version=CURRENT_VERSION_KEY,
            bundle_hash="ab" * 32,
        )
    )

    assert pushed == [["hk-v1"]]


async def test_unknown_synapse_tracking_evicts_after_twenty_rounds(
    mock_miner_config: bt.Config,
) -> None:
    from endure.protocol.synapses import SubmitCommit
    from endure.protocol.version_contract import CURRENT_VERSION_KEY
    from neurons.miner import Miner

    miner = Miner(config=mock_miner_config)
    miner.metagraph = _FakeMetagraph()
    miner.uid = 2

    async def fake_dendrite(*, axons, synapse, timeout, deserialize):  # noqa: ANN001, ANN202
        responses = []
        for _ in axons:
            response = synapse.model_copy()
            response.accepted = False
            response.dendrite = bt.TerminalInfo(
                status_code=404,
                status_message=(
                    f"Synapse name '{response.name}' not found. "
                    "Available synapses ['Synapse']"
                ),
            )
            responses.append(response)
        return responses

    miner.dendrite = fake_dendrite
    for day in range(1, 22):
        await miner._send(
            SubmitCommit(
                round_id=f"2023-03-{day:02d}",
                schema_id=RISK_SCHEMA_ID,
                spec_version=CURRENT_VERSION_KEY,
                bundle_hash="ab" * 32,
            )
        )

    assert len(miner._unknown_synapse) == 20
    assert "2023-03-01" not in miner._unknown_synapse
    assert "2023-03-21" in miner._unknown_synapse


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
        patch(
            "neurons.miner.install_shutdown_handlers", return_value=threading.Event()
        ),
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
        patch(
            "neurons.miner.install_shutdown_handlers", return_value=threading.Event()
        ),
        patch("neurons.miner.Miner", return_value=context),
        pytest.raises(SystemExit) as exit_info,
    ):
        main()

    assert exit_info.value.code == 1
    context.__exit__.assert_called_once()


def test_main_stops_cleanly_when_the_shutdown_event_is_set() -> None:
    from neurons.miner import main

    miner = MagicMock()
    context = MagicMock()
    context.__enter__.return_value = miner
    stop = threading.Event()
    stop.set()

    with (
        patch("neurons.miner.install_shutdown_handlers", return_value=stop),
        patch("neurons.miner.Miner", return_value=context),
    ):
        main()

    context.__exit__.assert_called_once()
    miner.thread.is_alive.assert_not_called()


def test_shutdown_timeout_keeps_push_thread_reference() -> None:
    from neurons.miner import Miner

    miner = Miner.__new__(Miner)
    miner.should_exit = False
    miner._shutdown_event = threading.Event()
    miner.is_running = True
    miner.axon = MagicMock()
    base_thread = MagicMock(spec=threading.Thread)
    base_thread.is_alive.return_value = False
    miner.thread = base_thread
    push_thread = MagicMock(spec=threading.Thread)
    push_thread.is_alive.return_value = True
    miner._push_thread = push_thread

    with pytest.raises(RuntimeError, match="miner shutdown incomplete"):
        miner.stop_run_thread()

    assert miner._push_thread is push_thread
    assert miner._shutdown_event.is_set()
    push_thread.join.assert_called_once_with(30.0)


def test_miner_resource_cleanup_closes_dendrite_and_subtensor() -> None:
    from neurons.miner import Miner

    miner = Miner.__new__(Miner)
    miner.dendrite = MagicMock()
    miner.subtensor = MagicMock()

    miner.close_transport_resources()

    miner.dendrite.close_session.assert_called_once_with()
    miner.subtensor.close.assert_called_once_with()
