#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Run a hermetic Alpha Risk compressed cycle against local Subtensor."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

import bittensor as bt

from endure.assessment.coordinates import AssessmentCoordinate
from endure.assessment.schemas.subnet_alpha_risk import (
    HORIZON_5D_SECONDS,
    HORIZON_30D_SECONDS,
    RISK_HORIZONS,
    RISK_SCHEMA_ID,
    RiskOutput,
)
from endure.assessment.subnet_alpha_universe import parse_alpha_risk_universe_members
from endure.storage.repository import Storage
from endure.utils.logging import safe_error
from neurons.validator import _run_migrations

FAULT_NONE = "none"
FAULT_MINER_RESTART = "miner-restart-after-commit"
FAULT_VALIDATOR_RESTART = "validator-restart-after-commit"
FAULT_MINER_STATE_LOSS = "miner-state-loss-after-commit"
FAULT_CHOICES = (
    FAULT_NONE,
    FAULT_MINER_RESTART,
    FAULT_VALIDATOR_RESTART,
    FAULT_MINER_STATE_LOSS,
)
# Faults that target the miner rather than the validator.
FAULT_MINER_ROLES = (FAULT_MINER_RESTART, FAULT_MINER_STATE_LOSS)
MIN_FAULT_ROUND_SECONDS = 240
# What the checklist reports when a commit reached the validator but no reveal
# ever did — the signature of a lost round (transport failure or state loss).
LOST_SUBMISSION_DETAIL = "commit observed; no reveal reached the handler"
READY_MARKERS = {
    "validator": "Validator starting at block:",
    "miner": "Miner starting at block:",
}
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


@dataclass(frozen=True, slots=True)
class DevnetCycleArgs:
    netuid: int
    network: str
    wallet_path: str
    validator_wallet: str
    miner_wallet: str
    hotkey: str
    timeout_seconds: int
    artifact_root: Path = Path("var/devnet-runs")
    run_id: str | None = None
    startup_timeout_seconds: int = 120
    startup_runway_seconds: int = 15
    round_seconds: int = 60
    horizon_5d_seconds: int = 5
    horizon_30d_seconds: int = 10
    epoch_length_blocks: int = 100
    poll_seconds: int = 1
    fault: str = FAULT_NONE


@dataclass(frozen=True, slots=True)
class RunLayout:
    root: Path
    database: Path
    state: Path
    validator_log: Path
    miner_log: Path


@dataclass(frozen=True, slots=True)
class AxonBinding:
    port: int
    # The IP to publish; None means "let the neuron auto-detect and serve its
    # own" — used to bootstrap a freshly-registered hotkey that has never served.
    external_ip: str | None


@dataclass(slots=True)
class ManagedNeuron:
    role: str
    command: list[str]
    log_path: Path
    process: subprocess.Popen[str]
    restarts: int = 0


@dataclass(frozen=True, slots=True)
class WeightStatus:
    confirmed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class Checklist:
    round_opened: bool
    bundle_accepted: bool
    submission_detail: str
    consensus_rows: int
    consensus_complete: bool
    five_day_pass: bool
    thirty_day_pass: bool
    round_closed: bool
    emas_positive: bool
    blended_score_positive: bool
    weights_confirmed: bool
    miner_weight_nonzero: bool
    weight_detail: str

    def complete(self) -> bool:
        return all(
            (
                self.round_opened,
                self.bundle_accepted,
                self.consensus_complete,
                self.five_day_pass,
                self.thirty_day_pass,
                self.round_closed,
                self.emas_positive,
                self.blended_score_positive,
                self.weights_confirmed,
                self.miner_weight_nonzero,
            )
        )

    def submission_lost(self) -> bool:
        """A commit reached the validator but the round closed with no reveal.

        This is the terminal state the miner-state-loss fault targets: the
        harness surfaces the silent zero as an explicit, named outcome instead
        of a round that only looks empty a full cycle later on testnet.
        """
        return (
            self.round_opened
            and self.round_closed
            and not self.bundle_accepted
            and self.submission_detail == LOST_SUBMISSION_DETAIL
        )


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def _parse_args(argv: list[str] | None = None) -> DevnetCycleArgs:
    parser = argparse.ArgumentParser()
    parser.add_argument("--netuid", type=int, required=True)
    parser.add_argument("--network", required=True)
    parser.add_argument(
        "--wallet-path", default=str(Path.home() / ".bittensor" / "wallets")
    )
    parser.add_argument("--validator-wallet", default="validator")
    parser.add_argument("--miner-wallet", default="miner")
    parser.add_argument("--hotkey", default="default")
    parser.add_argument("--timeout-seconds", type=_positive_int, default=360)
    parser.add_argument("--artifact-root", type=Path, default=Path("var/devnet-runs"))
    parser.add_argument("--run-id")
    parser.add_argument("--startup-timeout-seconds", type=_positive_int, default=120)
    parser.add_argument("--startup-runway-seconds", type=_positive_int, default=15)
    parser.add_argument("--round-seconds", type=_positive_int, default=60)
    parser.add_argument("--horizon-5d-seconds", type=_positive_int, default=5)
    parser.add_argument("--horizon-30d-seconds", type=_positive_int, default=10)
    parser.add_argument("--epoch-length-blocks", type=_positive_int, default=100)
    parser.add_argument("--poll-seconds", type=_positive_int, default=1)
    parser.add_argument("--fault", choices=FAULT_CHOICES, default=FAULT_NONE)
    ns = parser.parse_args(argv)
    return DevnetCycleArgs(
        netuid=ns.netuid,
        network=ns.network,
        wallet_path=ns.wallet_path,
        validator_wallet=ns.validator_wallet,
        miner_wallet=ns.miner_wallet,
        hotkey=ns.hotkey,
        timeout_seconds=ns.timeout_seconds,
        artifact_root=ns.artifact_root,
        run_id=ns.run_id,
        startup_timeout_seconds=ns.startup_timeout_seconds,
        startup_runway_seconds=ns.startup_runway_seconds,
        round_seconds=ns.round_seconds,
        horizon_5d_seconds=ns.horizon_5d_seconds,
        horizon_30d_seconds=ns.horizon_30d_seconds,
        epoch_length_blocks=ns.epoch_length_blocks,
        poll_seconds=ns.poll_seconds,
        fault=ns.fault,
    )


def _validate_args(args: DevnetCycleArgs) -> None:
    if args.netuid < 0:
        raise ValueError("netuid must be non-negative")
    if args.run_id is not None and _RUN_ID_RE.fullmatch(args.run_id) is None:
        raise ValueError(
            "run-id must contain only letters, digits, dot, dash, underscore"
        )
    if args.horizon_5d_seconds >= args.horizon_30d_seconds:
        raise ValueError("compressed 5d horizon must be before the 30d horizon")
    if args.horizon_30d_seconds >= args.round_seconds:
        raise ValueError("compressed horizons must fit inside the round period")
    if args.fault != FAULT_NONE and args.round_seconds < MIN_FAULT_ROUND_SECONDS:
        raise ValueError(
            f"fault runs require --round-seconds >= {MIN_FAULT_ROUND_SECONDS} "
            "so a restarted neuron can rejoin before the reveal window"
        )


def _default_run_id(now: datetime) -> str:
    return f"{now.strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}"


def _create_layout(args: DevnetCycleArgs, *, now: datetime) -> RunLayout:
    run_id = args.run_id or _default_run_id(now)
    root = args.artifact_root.expanduser().resolve() / run_id
    root.mkdir(parents=True, exist_ok=False)
    state = root / "state"
    state.mkdir()
    return RunLayout(
        root=root,
        database=root / "validator.db",
        state=state,
        validator_log=root / "validator.log",
        miner_log=root / "miner.log",
    )


def _require_available_port(port: int, *, hotkey: str) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as error:
            raise RuntimeError(
                f"local port {port} for hotkey {hotkey} is already in use; "
                "stop the other run or use a separately registered wallet"
            ) from error


def _free_local_port() -> int:
    """Reserve an ephemeral loopback port for a hotkey that has never served."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wallet_hotkey(args: DevnetCycleArgs, wallet_name: str) -> str:
    wallet = bt.Wallet(name=wallet_name, hotkey=args.hotkey, path=args.wallet_path)
    return str(wallet.hotkey.ss58_address)


def _resolve_bindings(
    args: DevnetCycleArgs, *, miner_hotkey: str, validator_hotkey: str
) -> tuple[AxonBinding, AxonBinding]:
    subtensor = bt.Subtensor(network=args.network)
    try:
        metagraph = subtensor.metagraph(netuid=args.netuid)
    finally:
        subtensor.close()
    hotkeys = list(metagraph.hotkeys)

    def binding(hotkey: str) -> AxonBinding:
        if hotkey not in hotkeys:
            raise RuntimeError(
                f"hotkey {hotkey} is not registered on netuid {args.netuid}"
            )
        axon = metagraph.axons[hotkeys.index(hotkey)]
        if not axon.is_serving or axon.port <= 0:
            # Fresh registration (seed_chain.sh registers but never serves): the
            # neuron has no on-chain endpoint yet. Bootstrap it on a free local
            # port and let it auto-detect and serve its own IP on first launch,
            # rather than failing the documented Phase 1-3 flow.
            return AxonBinding(port=_free_local_port(), external_ip=None)
        port = int(axon.port)
        _require_available_port(port, hotkey=hotkey)
        external_ip = str(axon.ip)
        return AxonBinding(port=port, external_ip=external_ip)

    miner = binding(miner_hotkey)
    validator = binding(validator_hotkey)
    if miner.port == validator.port:
        raise RuntimeError(
            "miner and validator are registered on the same local Axon port; "
            "seed distinct endpoints before running the cycle"
        )
    return miner, validator


def _axon_label(binding: AxonBinding) -> str:
    return f"{binding.external_ip or 'auto'}:{binding.port}"


def _neuron_command(  # noqa: PLR0913 - explicit runtime evidence controls
    *,
    role: str,
    args: DevnetCycleArgs,
    database_url: str,
    epoch: str,
    axon_port: int,
    external_ip: str | None = None,
    logging_dir: Path | None = None,
    validator_axon_override: str | None = None,
) -> list[str]:
    wallet = args.validator_wallet if role == "validator" else args.miner_wallet
    command = [
        sys.executable,
        f"neurons/{role}.py",
        "--endure.active_schema",
        RISK_SCHEMA_ID,
        "--endure.devnet_time_compression",
        "--endure.devnet_round_seconds",
        str(args.round_seconds),
        "--endure.devnet_horizon_5d_seconds",
        str(args.horizon_5d_seconds),
        "--endure.devnet_horizon_30d_seconds",
        str(args.horizon_30d_seconds),
        "--endure.database_url",
        database_url,
        "--endure.synthetic_epoch",
        epoch,
        "--endure.tick_seconds",
        "1",
        "--netuid",
        str(args.netuid),
        "--subtensor.chain_endpoint",
        args.network,
        "--subtensor.network",
        args.network,
        "--wallet.name",
        wallet,
        "--wallet.hotkey",
        args.hotkey,
        "--wallet.path",
        args.wallet_path,
        "--axon.port",
        str(axon_port),
        "--neuron.epoch_length",
        str(args.epoch_length_blocks),
        "--logging.debug",
    ]
    # A served hotkey pins its registered on-chain IP; a fresh one is left to
    # auto-detect and serve its own address (the local chain rejects loopback).
    if external_ip is not None:
        command.extend(("--axon.external_ip", external_ip))
    if logging_dir is not None:
        command.extend(("--logging.logging_dir", str(logging_dir)))
    if role == "miner" and validator_axon_override is not None:
        command.extend(("--endure.validator_axon_overrides", validator_axon_override))
    return command


def _start(command: list[str], log_path: Path) -> subprocess.Popen[str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("w", encoding="utf-8")
    try:
        return subprocess.Popen(  # noqa: S603 - operator argv assembled above.
            command,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    finally:
        handle.close()


def _stop(processes: tuple[subprocess.Popen[str], ...]) -> None:
    for process in processes:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
    for process in processes:
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def _require_alive(neurons: tuple[ManagedNeuron, ...]) -> None:
    for neuron in neurons:
        exit_code = neuron.process.poll()
        if exit_code is not None:
            raise RuntimeError(
                f"{neuron.role} exited with code {exit_code}; log: {neuron.log_path}"
            )


def _wait_for_ready(
    neuron: ManagedNeuron, *, deadline: float, poll_seconds: int
) -> None:
    marker = READY_MARKERS[neuron.role]
    while time.monotonic() < deadline:
        _require_alive((neuron,))
        if neuron.log_path.exists() and marker in neuron.log_path.read_text(
            encoding="utf-8", errors="replace"
        ):
            return
        time.sleep(poll_seconds)
    raise TimeoutError(
        f"{neuron.role} did not reach {marker!r}; log: {neuron.log_path}"
    )


def _weight_status(storage: Storage) -> WeightStatus:
    # The criterion is that this run's fresh database CONTAINS a confirmed batch.
    # Ask storage directly and uncapped: inspecting a bounded window of the most
    # recent batches would fail the run whenever many later attempts (successive
    # emission periods, or rate-limited ambiguous ones) followed the confirmation.
    if storage.has_confirmed_weight_emission(schema_id=RISK_SCHEMA_ID):
        return WeightStatus(True, "confirmed weight emission batch present")
    history = storage.weight_emission_history(RISK_SCHEMA_ID)
    if not history:
        return WeightStatus(False, "no run-scoped emission batch")
    newest = history[0]
    return WeightStatus(
        False, f"batch {newest['id']} confirmation={newest['confirmation_state']}"
    )


def _miner_state_path(*, layout: RunLayout, args: DevnetCycleArgs) -> Path:
    return (
        layout.state
        / args.miner_wallet
        / args.hotkey
        / f"netuid{args.netuid}"
        / "miner"
        / "risk_miner_state.json"
    )


def _expected_coordinates(
    storage: Storage, *, round_id: str
) -> frozenset[AssessmentCoordinate]:
    universe = storage.universe_for(round_id, RISK_SCHEMA_ID)
    if universe is None:
        return frozenset()
    netuids = parse_alpha_risk_universe_members(universe.tickers)
    return frozenset(
        AssessmentCoordinate.subnet_asset(
            netuid=netuid,
            horizon_seconds=horizon,
            output=output.value,
        )
        for netuid in netuids
        for horizon in RISK_HORIZONS
        for output in RiskOutput
    )


def _has_positive_blended_score(storage: Storage, *, miner_hotkey: str) -> bool:
    for batch in storage.weight_emission_history(RISK_SCHEMA_ID):
        if batch["confirmation_state"] != "confirmed":
            continue
        rows = batch["rows"]
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or row.get("miner_hotkey") != miner_hotkey:
                continue
            score = row.get("blended_score_text")
            if not isinstance(score, str):
                continue
            try:
                if Decimal(score) > 0:
                    return True
            except InvalidOperation:
                continue
    return False


def _has_nonzero_confirmed_miner_weight(storage: Storage, *, miner_hotkey: str) -> bool:
    for batch in storage.weight_emission_history(RISK_SCHEMA_ID):
        if batch["confirmation_state"] != "confirmed":
            continue
        rows = batch["rows"]
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or row.get("miner_hotkey") != miner_hotkey:
                continue
            weight_u16 = row.get("weight_u16")
            if isinstance(weight_u16, int) and not isinstance(weight_u16, bool):
                if weight_u16 > 0:
                    return True
    return False


def _miner_state_committed(
    *, layout: RunLayout, args: DevnetCycleArgs, round_id: str
) -> bool:
    state_path = _miner_state_path(layout=layout, args=args)
    if not state_path.exists():
        return False
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    round_state = payload.get(round_id)
    return isinstance(round_state, dict) and round_state.get("committed") is True


def _checklist(storage: Storage, *, round_id: str, miner_hotkey: str) -> Checklist:
    expected_coordinates = _expected_coordinates(storage, round_id=round_id)
    targets_5d = storage.assessment_realized_targets_for_horizon(
        round_id, RISK_SCHEMA_ID, HORIZON_5D_SECONDS
    )
    targets_30d = storage.assessment_realized_targets_for_horizon(
        round_id, RISK_SCHEMA_ID, HORIZON_30D_SECONDS
    )
    weight_status = _weight_status(storage)
    state = storage.round_state(round_id, RISK_SCHEMA_ID)
    accepted_bundles = storage.accepted_assessment_bundles(round_id, RISK_SCHEMA_ID)
    commit_hash = storage.committed_hash(round_id, RISK_SCHEMA_ID, miner_hotkey)
    reveal_count = storage.reveal_count(round_id, RISK_SCHEMA_ID, miner_hotkey)
    submission_detail = (
        f"accepted bundles {len(accepted_bundles)}"
        if accepted_bundles
        else (
            LOST_SUBMISSION_DETAIL
            if commit_hash is not None and reveal_count == 0
            else (
                f"commit observed; rejected reveal attempts {reveal_count}"
                if commit_hash is not None
                else "no commits observed"
            )
        )
    )
    consensus = storage.assessment_consensus_for(round_id, RISK_SCHEMA_ID)
    consensus_coordinates = {row.coordinate for row in consensus}
    targets_5d_coordinates = {target.coordinate for target in targets_5d}
    targets_30d_coordinates = {target.coordinate for target in targets_30d}
    expected_5d = {
        coordinate
        for coordinate in expected_coordinates
        if coordinate.horizon_value == HORIZON_5D_SECONDS
    }
    expected_30d = {
        coordinate
        for coordinate in expected_coordinates
        if coordinate.horizon_value == HORIZON_30D_SECONDS
    }
    emas = storage.assessment_ema_states(RISK_SCHEMA_ID)
    return Checklist(
        round_opened=state is not None,
        bundle_accepted=bool(accepted_bundles),
        submission_detail=submission_detail,
        consensus_rows=len(consensus),
        consensus_complete=bool(expected_coordinates)
        and expected_coordinates.issubset(consensus_coordinates),
        five_day_pass=bool(expected_5d)
        and expected_5d.issubset(targets_5d_coordinates),
        thirty_day_pass=bool(expected_30d)
        and expected_30d.issubset(targets_30d_coordinates),
        round_closed=state == "closed",
        emas_positive=bool(
            miner_emas := [
                score.ema
                for score in emas
                if score.miner_hotkey == miner_hotkey
                and score.coordinate in expected_coordinates
            ]
        )
        and all(ema > 0 for ema in miner_emas),
        blended_score_positive=_has_positive_blended_score(
            storage, miner_hotkey=miner_hotkey
        ),
        weights_confirmed=weight_status.confirmed,
        miner_weight_nonzero=_has_nonzero_confirmed_miner_weight(
            storage, miner_hotkey=miner_hotkey
        ),
        weight_detail=weight_status.detail,
    )


def _print_checklist(checklist: Checklist) -> None:
    rows = (
        ("round opened", checklist.round_opened),
        (checklist.submission_detail, checklist.bundle_accepted),
        (f"consensus rows {checklist.consensus_rows}", checklist.consensus_complete),
        ("5d pass", checklist.five_day_pass),
        ("30d pass", checklist.thirty_day_pass),
        ("round closed", checklist.round_closed),
        ("miner EMA positive", checklist.emas_positive),
        ("miner blended score positive", checklist.blended_score_positive),
        (checklist.weight_detail, checklist.weights_confirmed),
        ("miner confirmed weight non-zero", checklist.miner_weight_nonzero),
    )
    for label, ok in rows:
        print(f"[{'x' if ok else ' '}] {label}")


def _restart_fault_target(
    *,
    fault: str,
    neurons: dict[str, ManagedNeuron],
    layout: RunLayout,
    args: DevnetCycleArgs,
) -> None:
    """Apply the configured fault to its target neuron.

    Restart faults stop the target and relaunch it from the same command,
    reusing the run's database and (for the miner) its durable state; recovery
    is the tested behaviour. State loss instead deletes the miner's persisted
    preimage and leaves it stopped — modelling a lost/corrupted state volume
    from which the committed nonce can never be reproduced, so no reveal can
    follow. The stranded commit is the tested behaviour.
    """
    role = "miner" if fault in FAULT_MINER_ROLES else "validator"
    target = neurons[role]
    _stop((target.process,))
    if fault == FAULT_MINER_STATE_LOSS:
        state_path = _miner_state_path(layout=layout, args=args)
        state_path.unlink(missing_ok=True)
        print(
            f"[x] injected {fault}; wiped miner state and left it stopped", flush=True
        )
        return
    target.restarts += 1
    target.log_path = layout.root / f"{role}-restart-{target.restarts}.log"
    target.process = _start(target.command, target.log_path)
    print(f"[x] injected {fault}; restarted {role}", flush=True)


def _ready_to_inject(
    *,
    args: DevnetCycleArgs,
    storage: Storage,
    layout: RunLayout,
    miner_hotkey: str,
    round_id: str,
) -> bool:
    """Whether the configured fault's preconditions are all met.

    Every fault waits until the validator has recorded the commit; miner faults
    additionally wait for the miner's own state to record it, so a restart has a
    durable preimage to recover and a state-loss wipe destroys a real one.
    """
    if args.fault == FAULT_NONE:
        return False
    if storage.committed_hash(round_id, RISK_SCHEMA_ID, miner_hotkey) is None:
        return False
    if args.fault in FAULT_MINER_ROLES and not _miner_state_committed(
        layout=layout, args=args, round_id=round_id
    ):
        return False
    return True


def _inject_fault(
    *,
    args: DevnetCycleArgs,
    neurons: dict[str, ManagedNeuron],
    layout: RunLayout,
    deadline: float,
) -> None:
    _restart_fault_target(fault=args.fault, neurons=neurons, layout=layout, args=args)
    if args.fault == FAULT_MINER_STATE_LOSS:
        # The miner is intentionally left stopped; drop it from the liveness
        # set so the run can observe the validator strand the round.
        neurons.pop("miner", None)
        return
    restarted = (
        neurons["miner"] if args.fault in FAULT_MINER_ROLES else neurons["validator"]
    )
    _wait_for_ready(
        restarted,
        deadline=min(deadline, time.monotonic() + args.startup_timeout_seconds),
        poll_seconds=args.poll_seconds,
    )


def _run_succeeded(
    args: DevnetCycleArgs, checklist: Checklist, *, fault_injected: bool
) -> bool:
    """Terminal success for the run's fault mode.

    Every mode but state loss requires full recovery; state loss instead
    requires the lost round to be surfaced after the wipe was injected.
    """
    if args.fault == FAULT_MINER_STATE_LOSS:
        return fault_injected and checklist.submission_lost()
    return checklist.complete() and (args.fault == FAULT_NONE or fault_injected)


def _run(args: DevnetCycleArgs) -> int:
    _validate_args(args)
    started_at = datetime.now(UTC)
    layout = _create_layout(args, now=started_at)
    database_url = f"sqlite:///{layout.database}"
    _run_migrations(database_url)
    validator_hotkey = _wallet_hotkey(args, args.validator_wallet)
    miner_hotkey = _wallet_hotkey(args, args.miner_wallet)
    miner_binding, validator_binding = _resolve_bindings(
        args, miner_hotkey=miner_hotkey, validator_hotkey=validator_hotkey
    )
    epoch_dt = datetime.now(UTC) + timedelta(
        seconds=args.startup_timeout_seconds + args.startup_runway_seconds
    )
    epoch = epoch_dt.isoformat()
    round_id = epoch_dt.date().isoformat()
    override = f"{validator_hotkey}=127.0.0.1:{validator_binding.port}"
    commands = {
        "validator": _neuron_command(
            role="validator",
            args=args,
            database_url=database_url,
            epoch=epoch,
            axon_port=validator_binding.port,
            external_ip=validator_binding.external_ip,
            logging_dir=layout.state,
        ),
        "miner": _neuron_command(
            role="miner",
            args=args,
            database_url=database_url,
            epoch=epoch,
            axon_port=miner_binding.port,
            external_ip=miner_binding.external_ip,
            logging_dir=layout.state,
            validator_axon_override=override,
        ),
    }
    fault_injected = False
    checklist = Checklist(
        False,
        False,
        "no commits observed",
        0,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        "",
    )
    print(f"run: {layout.root}")
    print(f"round: {round_id} epoch={epoch}")
    print(
        "axons: "
        f"miner={_axon_label(miner_binding)} "
        f"validator={_axon_label(validator_binding)}"
    )
    # Every process is started INSIDE the try so a failure to launch the second
    # neuron or to open storage cannot orphan an already-running one.
    neurons: dict[str, ManagedNeuron] = {}
    try:
        for role in ("validator", "miner"):
            log_path = layout.validator_log if role == "validator" else layout.miner_log
            neurons[role] = ManagedNeuron(
                role, commands[role], log_path, _start(commands[role], log_path)
            )
        storage = Storage.from_url(database_url)
        deadline = time.monotonic() + args.timeout_seconds
        readiness_deadline = time.monotonic() + args.startup_timeout_seconds
        for neuron in neurons.values():
            _wait_for_ready(
                neuron,
                deadline=readiness_deadline,
                poll_seconds=args.poll_seconds,
            )
        runway = (epoch_dt - datetime.now(UTC)).total_seconds()
        if runway < args.startup_runway_seconds:
            raise RuntimeError(
                f"startup left only {runway:.1f}s before epoch; "
                f"required {args.startup_runway_seconds}s"
            )
        print(f"[x] both neurons ready with {runway:.1f}s epoch runway", flush=True)

        while time.monotonic() < deadline:
            _require_alive(tuple(neurons.values()))
            if not fault_injected and _ready_to_inject(
                args=args,
                storage=storage,
                layout=layout,
                miner_hotkey=miner_hotkey,
                round_id=round_id,
            ):
                _inject_fault(
                    args=args, neurons=neurons, layout=layout, deadline=deadline
                )
                fault_injected = True
            checklist = _checklist(
                storage, round_id=round_id, miner_hotkey=miner_hotkey
            )
            if _run_succeeded(args, checklist, fault_injected=fault_injected):
                _print_checklist(checklist)
                if args.fault == FAULT_MINER_STATE_LOSS:
                    print(
                        "[x] miner state loss surfaced as a stranded commit "
                        "(reveal never reached the validator)",
                        flush=True,
                    )
                return 0
            time.sleep(args.poll_seconds)
        _print_checklist(checklist)
        print(f"logs: {layout.validator_log} {layout.miner_log}")
        return 1
    finally:
        _stop(tuple(neuron.process for neuron in neurons.values()))


def main(argv: list[str] | None = None) -> int:
    try:
        return _run(_parse_args(argv))
    except Exception as error:  # noqa: BLE001 - CLI must cleanly report run failure.
        print(
            f"devnet cycle failed: {type(error).__name__}: {safe_error(error)}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
