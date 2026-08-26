from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import scripts.run_devnet_cycle as runner
from endure.assessment.coordinates import (
    AssessmentConsensusRow,
    AssessmentCoordinate,
    AssessmentEmaState,
    AssessmentRealizedTarget,
)
from endure.assessment.schemas.subnet_alpha_risk import RISK_HORIZONS, RiskOutput
from endure.assessment.universe import UniverseSnapshot
from endure.scoring.assessment_orchestrator import REALIZED_TARGET_RESOLVED
from endure.scoring.risk.orchestrator import risk_coordinate
from scripts.run_devnet_cycle import (
    FAULT_MINER_STATE_LOSS,
    FAULT_VALIDATOR_RESTART,
    LOST_SUBMISSION_DETAIL,
    Checklist,
    DevnetCycleArgs,
    ManagedNeuron,
    _checklist,
    _create_layout,
    _miner_state_committed,
    _miner_state_path,
    _neuron_command,
    _require_alive,
    _restart_fault_target,
    _validate_args,
    _weight_status,
)


def _checklist_row(
    *,
    round_opened: bool = True,
    bundle_accepted: bool = False,
    submission_detail: str = LOST_SUBMISSION_DETAIL,
    consensus_rows: int = 0,
    consensus_complete: bool = False,
    five_day_pass: bool = False,
    thirty_day_pass: bool = False,
    round_closed: bool = True,
    emas_positive: bool = False,
    blended_score_positive: bool = False,
    weights_confirmed: bool = False,
    miner_weight_nonzero: bool = False,
    weight_detail: str = "",
) -> Checklist:
    return Checklist(
        round_opened=round_opened,
        bundle_accepted=bundle_accepted,
        submission_detail=submission_detail,
        consensus_rows=consensus_rows,
        consensus_complete=consensus_complete,
        five_day_pass=five_day_pass,
        thirty_day_pass=thirty_day_pass,
        round_closed=round_closed,
        emas_positive=emas_positive,
        blended_score_positive=blended_score_positive,
        weights_confirmed=weights_confirmed,
        miner_weight_nonzero=miner_weight_nonzero,
        weight_detail=weight_detail,
    )


EXAMPLE_AXON_IP = ".".join(("192", "0", "2", "10"))


def _args(**overrides: object) -> DevnetCycleArgs:
    base = DevnetCycleArgs(
        netuid=2,
        network="ws://127.0.0.1:9946",
        wallet_path="/tmp/wallets",
        validator_wallet="validator",
        miner_wallet="miner",
        hotkey="default",
        timeout_seconds=360,
    )
    return replace(base, **overrides)


def _command(
    role: str,
    *,
    args: DevnetCycleArgs | None = None,
    external_ip: str | None = "127.0.0.1",
) -> list[str]:
    return _neuron_command(
        role=role,
        args=args or _args(),
        database_url="sqlite:///tmp.db",
        epoch="2026-07-07T00:00:00+00:00",
        axon_port=8092 if role == "validator" else 8091,
        external_ip=external_ip,
        logging_dir=Path("/tmp/run-state"),
        validator_axon_override="validator-hotkey=127.0.0.1:8092",
    )


def _r5_coordinates(netuids: tuple[int, ...]) -> tuple[AssessmentCoordinate, ...]:
    return tuple(
        risk_coordinate(netuid, horizon, output)
        for netuid in netuids
        for horizon in RISK_HORIZONS
        for output in RiskOutput
    )


def _r5_storage(
    *,
    consensus_coordinates: tuple[AssessmentCoordinate, ...],
    resolved_coordinates: tuple[AssessmentCoordinate, ...],
    ema_states: tuple[AssessmentEmaState, ...],
    voided_coordinates: tuple[AssessmentCoordinate, ...] = (),
    blended_score: Decimal | None = Decimal("0.5"),
    weight_u16: int = 1,
) -> MagicMock:
    storage = MagicMock()
    storage.universe_for.return_value = UniverseSnapshot(
        round_id="2026-08-20", tickers=("1", "3"), source_hash="test-universe"
    )
    storage.round_state.return_value = "closed"
    storage.accepted_assessment_bundles.return_value = [MagicMock()]
    storage.committed_hash.return_value = "ab" * 32
    storage.reveal_count.return_value = 1
    storage.assessment_consensus_for.return_value = [
        AssessmentConsensusRow(
            coordinate=coordinate,
            value=Decimal(1),
            dispersion=Decimal(0),
            n_submitters=1,
        )
        for coordinate in consensus_coordinates
    ]
    storage.assessment_realized_targets_for_horizon.side_effect = (
        lambda _round_id, _schema_id, horizon: [
            AssessmentRealizedTarget(
                coordinate=coordinate,
                value=Decimal(1),
                status=(
                    "voided"
                    if coordinate in voided_coordinates
                    else REALIZED_TARGET_RESOLVED
                ),
            )
            for coordinate in resolved_coordinates
            if coordinate.horizon_value == horizon
        ]
    )
    storage.assessment_ema_states.return_value = ema_states
    storage.has_confirmed_weight_emission.return_value = True
    storage.weight_emission_history.return_value = [
        {
            "id": 1,
            "confirmation_state": "confirmed",
            "rows": [
                {
                    "miner_hotkey": "miner-hotkey",
                    "blended_score_text": (
                        None if blended_score is None else str(blended_score)
                    ),
                    "weight_u16": weight_u16,
                }
            ],
        }
    ]
    return storage


def _positive_ema_states(
    coordinates: tuple[AssessmentCoordinate, ...],
    *,
    miner_hotkey: str = "miner-hotkey",
) -> tuple[AssessmentEmaState, ...]:
    return tuple(
        AssessmentEmaState(
            miner_hotkey=miner_hotkey,
            coordinate=coordinate,
            ema=Decimal("0.5"),
            resolved_rounds=1,
        )
        for coordinate in coordinates
    )


def test_r5_checklist_fails_when_a_consensus_coordinate_is_missing() -> None:
    coordinates = _r5_coordinates((1, 3))
    storage = _r5_storage(
        consensus_coordinates=coordinates[:-1],
        resolved_coordinates=coordinates,
        ema_states=_positive_ema_states(coordinates),
    )

    assert (
        _checklist(
            storage, round_id="2026-08-20", miner_hotkey="miner-hotkey"
        ).complete()
        is False
    )


def test_r5_checklist_fails_when_one_resolution_horizon_is_partial() -> None:
    coordinates = _r5_coordinates((1, 3))
    storage = _r5_storage(
        consensus_coordinates=coordinates,
        resolved_coordinates=coordinates[:-1],
        ema_states=_positive_ema_states(coordinates),
    )

    assert (
        _checklist(
            storage, round_id="2026-08-20", miner_hotkey="miner-hotkey"
        ).complete()
        is False
    )


def test_r5_checklist_accepts_voided_targets_when_every_coordinate_is_recorded() -> (
    None
):
    coordinates = _r5_coordinates((1, 3))
    storage = _r5_storage(
        consensus_coordinates=coordinates,
        resolved_coordinates=coordinates,
        voided_coordinates=tuple(
            coordinate for index, coordinate in enumerate(coordinates) if index % 2 == 0
        ),
        ema_states=_positive_ema_states(coordinates),
    )

    assert _checklist(
        storage, round_id="2026-08-20", miner_hotkey="miner-hotkey"
    ).complete()


def test_r5_checklist_fails_when_positive_ema_has_no_positive_blended_score() -> None:
    coordinates = _r5_coordinates((1, 3))
    storage = _r5_storage(
        consensus_coordinates=coordinates,
        resolved_coordinates=coordinates,
        ema_states=_positive_ema_states(coordinates),
        blended_score=Decimal(0),
    )

    assert (
        _checklist(
            storage, round_id="2026-08-20", miner_hotkey="miner-hotkey"
        ).complete()
        is False
    )


def test_r5_checklist_fails_when_confirmed_miner_weight_is_zero() -> None:
    coordinates = _r5_coordinates((1, 3))
    storage = _r5_storage(
        consensus_coordinates=coordinates,
        resolved_coordinates=coordinates,
        ema_states=_positive_ema_states(coordinates),
        weight_u16=0,
    )

    assert (
        _checklist(
            storage, round_id="2026-08-20", miner_hotkey="miner-hotkey"
        ).complete()
        is False
    )


def test_r5_checklist_fails_when_any_miner_ema_is_zero() -> None:
    coordinates = _r5_coordinates((1, 3))
    ema_states = list(_positive_ema_states(coordinates))
    ema_states[-1] = AssessmentEmaState(
        miner_hotkey="miner-hotkey",
        coordinate=coordinates[-1],
        ema=Decimal(0),
        resolved_rounds=1,
    )
    storage = _r5_storage(
        consensus_coordinates=coordinates,
        resolved_coordinates=coordinates,
        ema_states=tuple(ema_states),
    )

    assert (
        _checklist(
            storage, round_id="2026-08-20", miner_hotkey="miner-hotkey"
        ).complete()
        is False
    )


def test_r5_checklist_passes_with_complete_coverage_positive_blended_score_and_weights() -> (
    None
):
    coordinates = _r5_coordinates((1, 3))
    storage = _r5_storage(
        consensus_coordinates=coordinates,
        resolved_coordinates=coordinates,
        ema_states=_positive_ema_states(coordinates),
    )

    assert _checklist(
        storage, round_id="2026-08-20", miner_hotkey="miner-hotkey"
    ).complete()


def test_neuron_command_passes_local_endpoint_to_guarded_config() -> None:
    for role in ("miner", "validator"):
        command = _command(role)

        assert command[command.index("--subtensor.chain_endpoint") + 1] == (
            "ws://127.0.0.1:9946"
        )
        assert command[command.index("--subtensor.network") + 1] == (
            "ws://127.0.0.1:9946"
        )
        assert command[command.index("--axon.external_ip") + 1] == "127.0.0.1"


def test_neuron_command_isolates_state_and_routes_miner_to_validator() -> None:
    validator_command = _command("validator")
    miner_command = _command("miner")

    assert validator_command[validator_command.index("--wallet.name") + 1] == (
        "validator"
    )
    assert miner_command[miner_command.index("--wallet.name") + 1] == "miner"
    assert miner_command[miner_command.index("--logging.logging_dir") + 1] == (
        "/tmp/run-state"
    )
    assert (
        miner_command[miner_command.index("--endure.validator_axon_overrides") + 1]
        == "validator-hotkey=127.0.0.1:8092"
    )
    assert "--endure.validator_axon_overrides" not in validator_command


def test_neuron_command_uses_non_starving_epoch_pacing() -> None:
    command = _command("validator", args=_args(epoch_length_blocks=100))

    assert command[command.index("--neuron.epoch_length") + 1] == "100"


def test_neuron_command_omits_external_ip_when_bootstrapping() -> None:
    # A freshly-registered hotkey has no on-chain IP to reuse; the command must
    # not pin one, so the neuron auto-detects and serves its own on first run.
    command = _command("validator", external_ip=None)

    assert "--axon.external_ip" not in command


def test_bindings_reuse_registered_axon_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subtensor = MagicMock()
    subtensor.metagraph.return_value.hotkeys = ["miner-hotkey", "validator-hotkey"]
    subtensor.metagraph.return_value.axons = [
        MagicMock(is_serving=True, port=8091, ip=EXAMPLE_AXON_IP),
        MagicMock(is_serving=True, port=8092, ip=EXAMPLE_AXON_IP),
    ]
    monkeypatch.setattr(runner.bt, "Subtensor", lambda **_kwargs: subtensor)
    monkeypatch.setattr(
        runner, "_require_available_port", lambda *_args, **_kwargs: None
    )

    miner, validator = runner._resolve_bindings(
        _args(),
        miner_hotkey="miner-hotkey",
        validator_hotkey="validator-hotkey",
    )

    assert miner == runner.AxonBinding(port=8091, external_ip=EXAMPLE_AXON_IP)
    assert validator == runner.AxonBinding(port=8092, external_ip=EXAMPLE_AXON_IP)
    subtensor.close.assert_called_once()


def test_bindings_bootstrap_unserved_hotkey_on_a_free_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A freshly-registered but never-served miner must not fail the run: it is
    # bootstrapped on a free local port with no pinned IP (auto-serve on launch).
    subtensor = MagicMock()
    subtensor.metagraph.return_value.hotkeys = ["miner-hotkey", "validator-hotkey"]
    subtensor.metagraph.return_value.axons = [
        MagicMock(is_serving=False, port=0, ip=".".join(("0", "0", "0", "0"))),
        MagicMock(is_serving=True, port=8092, ip=EXAMPLE_AXON_IP),
    ]
    monkeypatch.setattr(runner.bt, "Subtensor", lambda **_kwargs: subtensor)
    monkeypatch.setattr(
        runner, "_require_available_port", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(runner, "_free_local_port", lambda: 45123)

    miner, validator = runner._resolve_bindings(
        _args(),
        miner_hotkey="miner-hotkey",
        validator_hotkey="validator-hotkey",
    )

    assert miner == runner.AxonBinding(port=45123, external_ip=None)
    assert validator == runner.AxonBinding(port=8092, external_ip=EXAMPLE_AXON_IP)


def test_create_layout_refuses_to_reuse_a_run_directory(tmp_path: Path) -> None:
    args = _args(artifact_root=tmp_path, run_id="run-1")
    now = datetime(2026, 8, 20, tzinfo=UTC)

    layout = _create_layout(args, now=now)

    assert layout.database == tmp_path / "run-1" / "validator.db"
    assert layout.state.is_dir()
    with pytest.raises(FileExistsError):
        _create_layout(args, now=now)


def test_validate_args_requires_restart_safe_fault_window() -> None:
    with pytest.raises(ValueError, match="round-seconds >= 240"):
        _validate_args(_args(fault=FAULT_VALIDATOR_RESTART, round_seconds=120))

    _validate_args(_args(fault=FAULT_VALIDATOR_RESTART, round_seconds=240))


def test_weight_status_requires_confirmed_batch_from_fresh_storage() -> None:
    storage = MagicMock()
    storage.has_confirmed_weight_emission.return_value = False
    storage.weight_emission_history.return_value = []
    assert _weight_status(storage).confirmed is False

    storage.weight_emission_history.return_value = [
        {"id": 7, "confirmation_state": "submitted"}
    ]
    submitted = _weight_status(storage)
    assert submitted.confirmed is False
    assert submitted.detail == "batch 7 confirmation=submitted"

    # Any confirmed batch, however old, satisfies the criterion — the storage
    # check is uncapped, so it is never hidden behind newer unconfirmed attempts.
    storage.has_confirmed_weight_emission.return_value = True
    status = _weight_status(storage)
    assert status.confirmed is True
    assert status.detail == "confirmed weight emission batch present"


def test_weight_status_reads_confirmation_uncapped_not_from_a_window() -> None:
    # Regression: the confirmed check must not depend on a bounded history read.
    storage = MagicMock()
    storage.has_confirmed_weight_emission.return_value = True
    storage.weight_emission_history.side_effect = AssertionError(
        "confirmed batches must be read uncapped, not from a recent window"
    )

    assert _weight_status(storage).confirmed is True
    storage.has_confirmed_weight_emission.assert_called_once_with(
        schema_id="risk.v1.subnet_alpha"
    )


def test_miner_restart_fault_requires_durable_committed_preimage(
    tmp_path: Path,
) -> None:
    args = _args(artifact_root=tmp_path, run_id="durability")
    layout = _create_layout(args, now=datetime(2026, 8, 20, tzinfo=UTC))
    state_path = (
        layout.state
        / "miner"
        / "default"
        / "netuid2"
        / "miner"
        / "risk_miner_state.json"
    )
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"2026-08-20":{"committed":false}}', encoding="utf-8")
    assert (
        _miner_state_committed(layout=layout, args=args, round_id="2026-08-20") is False
    )

    state_path.write_text('{"2026-08-20":{"committed":true}}', encoding="utf-8")
    assert _miner_state_committed(layout=layout, args=args, round_id="2026-08-20")


def test_checklist_keeps_closed_empty_round_visible() -> None:
    storage = MagicMock()
    storage.round_state.return_value = "closed"
    storage.assessment_realized_targets_for_horizon.return_value = []
    storage.accepted_assessment_bundles.return_value = []
    storage.committed_hash.return_value = None
    storage.reveal_count.return_value = 0
    storage.assessment_consensus_for.return_value = []
    storage.assessment_ema_states.return_value = []
    storage.has_confirmed_weight_emission.return_value = False
    storage.weight_emission_history.return_value = []

    checklist = _checklist(storage, round_id="2026-08-20", miner_hotkey="miner-hotkey")

    assert checklist.round_opened is True
    assert checklist.round_closed is True
    assert checklist.bundle_accepted is False
    assert checklist.submission_detail == "no commits observed"
    storage.round_state.assert_called_once_with("2026-08-20", "risk.v1.subnet_alpha")


def test_checklist_distinguishes_commit_without_reveal() -> None:
    storage = MagicMock()
    storage.round_state.return_value = "closed"
    storage.assessment_realized_targets_for_horizon.return_value = []
    storage.accepted_assessment_bundles.return_value = []
    storage.committed_hash.return_value = "ab" * 32
    storage.reveal_count.return_value = 0
    storage.assessment_consensus_for.return_value = []
    storage.assessment_ema_states.return_value = []
    storage.has_confirmed_weight_emission.return_value = False
    storage.weight_emission_history.return_value = []

    checklist = _checklist(storage, round_id="2026-08-20", miner_hotkey="miner-hotkey")

    assert (
        checklist.submission_detail == "commit observed; no reveal reached the handler"
    )


def test_validate_args_accepts_state_loss_fault_in_the_restart_window() -> None:
    with pytest.raises(ValueError, match="round-seconds >= 240"):
        _validate_args(_args(fault=FAULT_MINER_STATE_LOSS, round_seconds=120))

    _validate_args(_args(fault=FAULT_MINER_STATE_LOSS, round_seconds=240))


def test_submission_lost_only_when_closed_round_has_a_stranded_commit() -> None:
    assert _checklist_row().submission_lost() is True
    # An accepted bundle means the reveal landed — not a lost submission.
    assert (
        _checklist_row(
            bundle_accepted=True, submission_detail="accepted bundles 1"
        ).submission_lost()
        is False
    )
    # No commit at all is non-participation, not a stranded commit.
    assert (
        _checklist_row(submission_detail="no commits observed").submission_lost()
        is False
    )
    # Still open: the reveal window may not have run yet.
    assert _checklist_row(round_closed=False).submission_lost() is False


def test_state_loss_fault_wipes_preimage_and_leaves_the_miner_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _args(artifact_root=tmp_path, run_id="state-loss")
    layout = _create_layout(args, now=datetime(2026, 8, 20, tzinfo=UTC))
    state_path = _miner_state_path(layout=layout, args=args)
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"2026-08-20":{"committed":true}}', encoding="utf-8")

    stopped: list[object] = []
    monkeypatch.setattr(runner, "_stop", lambda procs: stopped.extend(procs))
    started: list[object] = []
    monkeypatch.setattr(runner, "_start", lambda *_a, **_k: started.append(1))
    original = MagicMock()
    neuron = ManagedNeuron(
        role="miner",
        command=["python", "neurons/miner.py"],
        log_path=layout.miner_log,
        process=original,
    )

    _restart_fault_target(
        fault=FAULT_MINER_STATE_LOSS,
        neurons={"miner": neuron},
        layout=layout,
        args=args,
    )

    # The committed preimage is gone, the miner was stopped, and — unlike a
    # restart fault — it was never relaunched: the round is left stranded.
    assert not state_path.exists()
    assert stopped == [original]
    assert started == []
    assert neuron.restarts == 0


def test_inject_fault_drops_the_stopped_miner_from_the_liveness_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _args(
        artifact_root=tmp_path, run_id="state-loss-inject", fault=FAULT_MINER_STATE_LOSS
    )
    layout = _create_layout(args, now=datetime(2026, 8, 20, tzinfo=UTC))
    _miner_state_path(layout=layout, args=args).parent.mkdir(parents=True)

    monkeypatch.setattr(runner, "_stop", lambda *_a, **_k: None)
    waited: list[object] = []
    monkeypatch.setattr(runner, "_wait_for_ready", lambda *a, **k: waited.append(1))
    neurons = {
        "validator": ManagedNeuron("validator", [], layout.validator_log, MagicMock()),
        "miner": ManagedNeuron("miner", [], layout.miner_log, MagicMock()),
    }

    runner._inject_fault(args=args, neurons=neurons, layout=layout, deadline=1.0)

    assert "miner" not in neurons
    assert "validator" in neurons
    assert waited == []


def test_require_alive_fails_fast_with_role_and_log() -> None:
    process = MagicMock()
    process.poll.return_value = 17
    neuron = ManagedNeuron(
        role="miner",
        command=["python", "neurons/miner.py"],
        log_path=Path("/tmp/miner.log"),
        process=process,
    )

    with pytest.raises(RuntimeError, match="miner exited with code 17"):
        _require_alive((neuron,))


def test_run_stops_the_started_neuron_when_a_later_launch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _args(artifact_root=tmp_path, run_id="p2b")
    layout = _create_layout(args, now=datetime(2026, 8, 20, tzinfo=UTC))
    monkeypatch.setattr(runner, "_create_layout", lambda a, *, now: layout)
    monkeypatch.setattr(runner, "_run_migrations", lambda _url: None)
    monkeypatch.setattr(runner, "_wallet_hotkey", lambda _a, wallet: f"{wallet}-hotkey")
    monkeypatch.setattr(
        runner,
        "_resolve_bindings",
        lambda *_a, **_k: (
            runner.AxonBinding(port=8091, external_ip=None),
            runner.AxonBinding(port=8092, external_ip=None),
        ),
    )
    validator_proc = MagicMock()

    def fake_start(command: list[str], _log_path: Path) -> object:
        if command[1].endswith("validator.py"):
            return validator_proc
        raise RuntimeError("miner launch failed")

    monkeypatch.setattr(runner, "_start", fake_start)
    stopped: list[tuple[object, ...]] = []
    monkeypatch.setattr(runner, "_stop", lambda procs: stopped.append(tuple(procs)))

    # The validator launches, the miner launch raises; the started validator
    # must still be stopped so it cannot orphan its Axon port.
    with pytest.raises(RuntimeError, match="miner launch failed"):
        runner._run(args)

    assert stopped == [(validator_proc,)]


def test_main_redacts_endpoint_credentials_from_failures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    credential_url = "".join(
        (
            "wss://devnet-user:devnet-password",
            "@rpc.example.invalid/ws?api_key=devnet-token",
        )
    )
    monkeypatch.setattr(runner, "_parse_args", lambda _argv: _args())
    monkeypatch.setattr(
        runner,
        "_run",
        MagicMock(side_effect=RuntimeError(f"connection failed: {credential_url}")),
    )

    assert runner.main([]) == 1

    stderr = capsys.readouterr().err
    assert "devnet-password" not in stderr
    assert "devnet-token" not in stderr
    assert "connection failed: <redacted-endpoint>" in stderr
