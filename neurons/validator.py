"""Endure validator entrypoint for schema-routed risk assessment.

Alpha Risk (``risk.v1.subnet_alpha``) is the served vertical. Forge lending
remains a dormant reference vertical
selectable with ``--endure.active_schema``. The validator serves the
commit/reveal axon, drives the selected round service every tick (open → embargo
→ resolve → score → close), and replaces the score vector with blended miner
EMAs whenever scoring happens.
"""

import asyncio
import contextlib
import copy
import os
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    ClassVar,
    Final,
    Literal,
    Protocol,
    Tuple,
    runtime_checkable,
)

import bittensor as bt

if TYPE_CHECKING:
    import uvicorn
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from fastapi import Request

from endure.api.app import PublicationIdentity, RuntimeHealth
from endure.assessment.registry import default_registry
from endure.assessment.schemas.forge_lending import FORGE_LENDING_SCHEMA_ID
from endure.assessment.schemas.subnet_alpha_risk import (
    HORIZON_5D_SECONDS,
    HORIZON_30D_SECONDS,
    RISK_HORIZONS,
    RISK_SCHEMA_ID,
)
from endure.assessment.subnet_alpha_universe import StaticAlphaRiskUniverseProvider
from endure.base.axon import authenticated_hotkey
from endure.base.shutdown import (
    StartupShutdownGuard,
    install_shutdown_handlers,
    join_thread_or_raise,
    run_entrypoint,
    terminate_process,
)
from endure.base.validator import (
    WEIGHT_EMISSION_FINALITY_MARGIN_BLOCKS,
    WEIGHT_EMISSION_PERIOD_BLOCKS,
    BaseValidatorNeuron,
    WeightEmissionAttempt,
    normalize_commitment_hash,
)
from endure.live.alpha_market_data import (
    LiveAlphaPriceProvider,
    LiveAlphaPriceProviderConfig,
    read_chain_genesis,
    validate_mainnet_archive,
)
from endure.protocol.admission import miner_admission
from endure.protocol.consensus_policy import OwnerVoteNetwork
from endure.protocol.handlers import SubmissionHandlers
from endure.protocol.risk_runtime import (
    RECORDED_FIXTURE_WINDOW_START_BLOCK,
    build_risk_devnet_runtime,
    compression_enabled,
)
from endure.protocol.schedulers import scheduler_for_schema
from endure.protocol.synapses import SubmitCommit, SubmitReveal
from endure.protocol.validator_service import ValidatorRoundService
from endure.protocol.version_contract import CURRENT_VERSION_KEY
from endure.protocol.vertical import AssessmentRoundProgram, VerticalRuntime
from endure.runtime.identity import runtime_identity
from endure.runtime.resolve import resolve_runtime_provider
from endure.scoring.assessment_orchestrator import ResolutionBudget
from endure.scoring.eligibility import DeregistrationTracker, scoring_set
from endure.scoring.emission_policy import (
    CHAIN_SNAPSHOT_METAGRAPH_INDICES,
    ChainSnapshot,
    EmissionBlocked,
    EmissionBlockReason,
    EmissionPlan,
    OwnerVoteRecipient,
    plan_emission,
    recheck_owner_vote,
    select_emission_mode,
)
from endure.scoring.market_data import recorded_mainnet_fixture_provider
from endure.scoring.policy import DEFAULT_PAYOUT_HALF_LIFE_ROUNDS
from endure.scoring.risk.orchestrator import RiskScoringOrchestrator
from endure.storage.repository import (
    CR4_REVEAL_SCAN_BATCH_BLOCKS,
    Storage,
    WeightCommitEvidence,
    WeightEmissionChainSnapshot,
    WeightEmissionRow,
    WeightRevealEvidence,
    ensure_sqlite_parent_dir,
)
from endure.utils.config import (
    DevOnlyConfigError,
    active_runtime_schema_id,
    apply_consensus_settings,
    owner_vote_network,
    permits_dev_only_runtime,
    require_compression_runtime_allowed,
    require_explicit_netuid,
    require_mainnet_validator_policy,
    require_serving_stage_allowed,
    resolve_chain_identity,
    uses_mainnet_consensus_policy,
)
from endure.utils.log_shipping import configure_log_shipping
from endure.utils.logging import safe_endpoint_label, safe_error

_RECORDED_FIXTURE_NETUIDS: Final = (8, 44)
ZERO = Decimal("0")
# Owner-state failures that no retry fixes on its own degrade /health at once;
# snapshot/identity glitches only after they persist for a couple of epochs.
# Blocked emission lets weights age toward activity_cutoff (5000 blocks on SN30).
_IMMEDIATE_EMISSION_BLOCKS: Final = frozenset(
    {"owner_hotkey_mismatch", "owner_unregistered", "owner_vote_chain_mismatch"}
)
_TRANSIENT_EMISSION_BLOCK_EPOCHS: Final = 2


def _cr4_reveal_scan_batch_budget(epoch_length: int) -> int:
    normal_catchup = (
        epoch_length + 1 + CR4_REVEAL_SCAN_BATCH_BLOCKS - 1
    ) // CR4_REVEAL_SCAN_BATCH_BLOCKS
    return normal_catchup + 1


@runtime_checkable
class _ScalarBlock(Protocol):
    def item(self) -> object: ...


def _cached_block_number(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, _ScalarBlock):
        return None
    try:
        scalar = value.item()
    except (TypeError, ValueError):
        return None
    return scalar if isinstance(scalar, int) and not isinstance(scalar, bool) else None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _run_migrations(database_url: str) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    config = AlembicConfig(str(repo_root / "alembic.ini"))
    config.set_main_option(
        "script_location", str(repo_root / "endure/storage/migrations")
    )
    config.set_main_option("sqlalchemy.url", database_url)
    ensure_sqlite_parent_dir(database_url)
    alembic_command.upgrade(config, "head")


def _require_hotkey(config: bt.Config) -> None:
    hotkey = bt.Wallet(config=config).hotkey.ss58_address
    if not hotkey:
        raise RuntimeError("the configured validator hotkey has no address")


class Validator(BaseValidatorNeuron):
    """Schema-routed validator round loop."""

    # Emission health bookkeeping is shared by the run loop and the API thread.
    _emission_state_lock: ClassVar[threading.RLock] = threading.RLock()

    def __init__(self, config: bt.Config | None = None) -> None:
        resolved_config = copy.deepcopy(config or type(self).build_config())
        # Endpoint names cannot identify an operator's own Finney node behind
        # loopback or a tunnel; genesis does, before any policy gate runs.
        resolve_chain_identity(resolved_config, read_genesis=read_chain_genesis)
        require_serving_stage_allowed(resolved_config)
        require_explicit_netuid(resolved_config)
        apply_consensus_settings(resolved_config)
        require_mainnet_validator_policy(resolved_config)
        if compression_enabled(resolved_config):
            # Refuse offline before the network-bound archive probe below.
            require_compression_runtime_allowed(resolved_config)
        if (
            active_runtime_schema_id(resolved_config) == RISK_SCHEMA_ID
            and int(resolved_config.neuron.num_concurrent_forwards) != 1
        ):
            raise RuntimeError(
                "risk.v1.subnet_alpha requires "
                "--neuron.num_concurrent_forwards 1; concurrent forwards "
                "bypass round-tick storage serialization"
            )
        if int(resolved_config.endure.health_tick_max_age_seconds) <= int(
            resolved_config.endure.tick_seconds
        ):
            raise RuntimeError(
                "endure.health_tick_max_age_seconds must be greater than "
                "endure.tick_seconds"
            )
        if int(resolved_config.endure.health_startup_grace_seconds) <= int(
            resolved_config.endure.tick_seconds
        ):
            raise RuntimeError(
                "endure.health_startup_grace_seconds must be greater than "
                "endure.tick_seconds"
            )
        if int(resolved_config.endure.health_tick_max_duration_seconds) <= int(
            resolved_config.endure.health_tick_max_age_seconds
        ):
            raise RuntimeError(
                "endure.health_tick_max_duration_seconds must be greater than "
                "endure.health_tick_max_age_seconds"
            )
        if int(resolved_config.endure.resolution_budget_seconds) >= int(
            resolved_config.endure.health_tick_max_duration_seconds
        ):
            raise RuntimeError(
                "endure.resolution_budget_seconds must be less than "
                "endure.health_tick_max_duration_seconds; a budget at or above "
                "the watchdog window cannot prevent stale-tick restarts"
            )
        # Local inputs fail offline, before the network-bound archive probe:
        # the SQLite URL/path (and schema), then the mainnet hotkey file.
        _run_migrations(resolved_config.endure.database_url)
        if uses_mainnet_consensus_policy(resolved_config):
            _require_hotkey(resolved_config)
            validate_mainnet_archive(
                str(resolved_config.endure.market_data_endpoint), netuid=30
            )
        super().__init__(
            config=resolved_config,
            runtime_provider=resolve_runtime_provider(resolved_config),
        )
        self._schema_id = active_runtime_schema_id(self.config)
        self._storage = Storage.from_url(self.config.endure.database_url)
        self._weight_emission_startup_fence_block: int | None = None
        self._owner_vote_recipient: OwnerVoteRecipient | None = None
        self._emission_block: EmissionBlockReason | None = None
        self._emission_block_since_block: int | None = None
        self._emission_block_seen_block: int | None = None
        self._emission_block_underlying: EmissionBlockReason | None = None
        self._emission_mode = (
            "disabled" if self.config.neuron.disable_set_weights else "abstain"
        )
        self._emission_reason = "initializing"
        self._emission_expected_since: float | None = None
        self._emission_deadline: float | None = None
        self._emission_next_eligible_block: int | None = None
        self._emission_chain_due_block: int | None = None
        self._emission_blocked_reason: str | None = None
        self._handlers = SubmissionHandlers(
            storage=self._storage,
            schema_id=self._schema_id,
            now_fn=_utc_now,
            max_commits_per_round=int(self.config.endure.max_commits_per_round),
            max_reveals_per_round=int(self.config.endure.max_reveals_per_round),
        )
        self._blended_snapshot: dict[str, Decimal] = {}
        self._vertical_runtime: VerticalRuntime
        self._service = self._build_service()
        self._reconstruct_scores()
        self._durable_scores_loaded = True
        self._seed_deregistration_tracker()
        self._tick_failures = 0
        self._last_tick_ok: str | None = None
        self._last_tick_monotonic: float | None = None
        self._last_tick_error: str | None = None
        # Anchors the watchdog's generous window while a long tick/sync is in
        # flight; None when the loop is between operations. See _tick_stale.
        self._long_op_started_monotonic: float | None = None
        self._started_monotonic = time.monotonic()
        self._current_tick_budget = ResolutionBudget.unlimited()
        self._process_started_at = _utc_now().isoformat()
        self._api_server: uvicorn.Server | None = None
        self._api_thread: threading.Thread | None = None
        self._attach_handlers()
        self._start_api()

    def runtime_health(self) -> RuntimeHealth:
        """Stuck-loop observability, merged into /health. Tick fields are the
        validator's; universe-fetch fields come from the round service (a
        failed open is swallowed there but must still surface as degraded).

        The API thread and the run loop both touch emission bookkeeping; one
        lock gives every response a consistent emission snapshot.
        """
        with self._emission_state():
            return self._runtime_health_snapshot()

    def _runtime_health_snapshot(self) -> RuntimeHealth:
        gate = self.rpc_gate.snapshot()
        storage = getattr(self, "_storage", None)
        metagraph_block = vars(self.metagraph).get("block")
        current_block = _cached_block_number(metagraph_block)
        confirmation = (
            None
            if storage is None
            else storage.weight_emission_confirmation_health(
                schema_id=self._schema_id, current_block=current_block
            )
        )
        latest_unconfirmed = (
            None
            if confirmation is None
            else confirmation.latest_unconfirmed_submission_block
        )
        latest_confirmed = (
            None
            if confirmation is None
            else confirmation.latest_confirmed_submission_block
        )
        unresolved_unconfirmed = latest_unconfirmed is not None and (
            latest_confirmed is None or latest_unconfirmed > latest_confirmed
        )
        deadline_overdue = (
            confirmation is not None
            and current_block is not None
            and confirmation.oldest_open_deadline_block is not None
            and current_block > confirmation.oldest_open_deadline_block
        )
        fallback_overdue = (
            confirmation is not None
            and confirmation.oldest_open_deadline_block is None
            and confirmation.oldest_open_age_blocks is not None
            and confirmation.oldest_open_age_blocks
            > WEIGHT_EMISSION_PERIOD_BLOCKS + WEIGHT_EMISSION_FINALITY_MARGIN_BLOCKS
        )
        unknown_block_open = (
            confirmation is not None
            and confirmation.open_submissions > 0
            and current_block is None
            and time.monotonic() - self._started_monotonic
            > int(self.config.endure.health_startup_grace_seconds)
        )
        self._refresh_emission_health(
            current_block,
            open_confirmation=(
                confirmation is not None and confirmation.open_submissions > 0
            ),
        )
        submission_overdue = (
            self._emission_deadline is not None
            and time.monotonic() > self._emission_deadline
        )
        weight_emission_degraded = (
            gate.degraded
            or gate.abandoned_generations > 0
            or self._consecutive_set_weights_failures > 0
            or unresolved_unconfirmed
            or deadline_overdue
            or fallback_overdue
            or unknown_block_open
            or submission_overdue
            or self._emission_block_degraded()
        )
        long_op_started = getattr(self, "_long_op_started_monotonic", None)
        return {
            "process_started_at": self._process_started_at,
            "process_uptime_seconds": int(time.monotonic() - self._started_monotonic),
            "validator_loop_alive": self._validator_loop_alive(),
            "tick_stale": self._tick_stale(),
            "seconds_since_last_tick": self._seconds_since_last_tick(),
            "long_op_in_flight": long_op_started is not None,
            "seconds_since_long_op_start": (
                None if long_op_started is None else time.monotonic() - long_op_started
            ),
            "consecutive_tick_failures": self._tick_failures,
            "last_tick_ok": self._last_tick_ok,
            "last_tick_error": self._last_tick_error,
            "consecutive_universe_failures": self._service.consecutive_universe_failures,
            "last_universe_error": self._service.last_universe_error,
            "consecutive_resolution_failures": (
                self._service.consecutive_resolution_failures
            ),
            "last_resolution_error": self._service.last_resolution_error,
            "consecutive_empty_scored_rounds": (
                self._service.consecutive_empty_scored_rounds
            ),
            "last_empty_scored_round": self._service.last_empty_scored_round,
            "assessment_due_seconds": (
                {
                    HORIZON_5D_SECONDS: int(
                        self.config.endure.devnet_horizon_5d_seconds
                    ),
                    HORIZON_30D_SECONDS: int(
                        self.config.endure.devnet_horizon_30d_seconds
                    ),
                }
                if compression_enabled(self.config)
                else {}
            ),
            "overdue_grace_seconds": int(
                self.config.endure.health_tick_max_duration_seconds
            ),
            "last_set_weights_ok": self._last_set_weights_ok,
            "consecutive_set_weights_failures": self._consecutive_set_weights_failures,
            "weight_emission_degraded": weight_emission_degraded,
            "emission_mode": self._emission_mode,
            "emission_reason": self._emission_reason,
            "emission_blocked_reason": (
                getattr(self, "_emission_blocked_reason", None)
                or getattr(self, "_emission_block", None)
            ),
            "emission_expected": self._emission_expected_since is not None,
            "emission_next_eligible_block": self._emission_next_eligible_block,
            "emission_expected_seconds": (
                None
                if self._emission_expected_since is None
                else max(0.0, time.monotonic() - self._emission_expected_since)
            ),
            "emission_deadline_in_seconds": (
                None
                if self._emission_deadline is None
                else self._emission_deadline - time.monotonic()
            ),
            "emission_submission_overdue": submission_overdue,
            "emission_confirmation_deadline_block": (
                None
                if confirmation is None
                else confirmation.oldest_open_deadline_block
            ),
            "last_confirmed_weights_at": (
                None if confirmation is None else confirmation.last_confirmed_at
            ),
            "open_weight_submissions": (
                0 if confirmation is None else confirmation.open_submissions
            ),
            "oldest_open_weight_submission_age_blocks": (
                None if confirmation is None else confirmation.oldest_open_age_blocks
            ),
            "latest_unconfirmed_weight_submission_block": (
                None
                if confirmation is None
                else confirmation.latest_unconfirmed_submission_block
            ),
            "failed_weight_submissions_total": (
                0 if confirmation is None else confirmation.failed_submissions_total
            ),
            "rpc_gate": {
                "adaptive_rate": gate.adaptive_rate,
                "degraded": gate.degraded,
                "rate_limited_total": gate.rate_limited_total,
                "deferred_total": gate.deferred_total,
                "abandoned_generations": gate.abandoned_generations,
            },
        }

    def _validator_loop_alive(self) -> bool:
        thread = getattr(self, "thread", None)
        return thread is not None and bool(thread.is_alive())

    def _seconds_since_last_tick(self) -> float | None:
        if self._last_tick_monotonic is None:
            return None
        return time.monotonic() - self._last_tick_monotonic

    def _new_tick_budget(self) -> ResolutionBudget:
        self._current_tick_budget = ResolutionBudget.starting_now(
            int(self.config.endure.resolution_budget_seconds)
        )
        return self._current_tick_budget

    def _tick_budget_exhausted(self) -> bool:
        return self._current_tick_budget.exhausted()

    def _mark_tick_progress(self) -> None:
        """Refresh tick liveness from bounded in-tick work, so a long catch-up
        tick survives the watchdog while a wedged thread still trips it."""
        self._last_tick_monotonic = time.monotonic()

    def _begin_long_op(self) -> None:
        # Keep the earliest anchor if a long operation is already in flight, so
        # the generous window measures the whole run, not the latest bracket.
        # getattr tolerates the base constructor's first sync(), which runs
        # before Validator.__init__ finishes declaring its state fields.
        if getattr(self, "_long_op_started_monotonic", None) is None:
            self._long_op_started_monotonic = time.monotonic()

    def _end_long_op(self) -> None:
        # Heartbeat before clearing the marker: a watchdog read between the
        # two writes must see a fresh tick, not the pre-long-op age with the
        # generous-window marker already gone.
        self._mark_tick_progress()
        self._long_op_started_monotonic = None

    def sync(self):
        # Every chain RPC inside sync() is deadline-bounded by the rpc gate, so
        # bracketing it with liveness marks is honest: a wedged gate operation
        # still raises within its deadline instead of marking forever, while a
        # slow-but-bounded sync no longer stacks its silence onto the tail of a
        # long forward pass.
        self._mark_tick_progress()
        self._begin_long_op()
        try:
            super().sync()
        finally:
            self._end_long_op()
            self._mark_tick_progress()

    def _tick_stale(self) -> bool:
        now = time.monotonic()
        long_op_started = getattr(self, "_long_op_started_monotonic", None)
        if long_op_started is not None:
            return now - long_op_started > int(
                self.config.endure.health_tick_max_duration_seconds
            )
        if self._last_tick_monotonic is None:
            return now - self._started_monotonic > int(
                self.config.endure.health_startup_grace_seconds
            )
        return now - self._last_tick_monotonic > int(
            self.config.endure.health_tick_max_age_seconds
        )

    def watchdog_exit_reason(self) -> str | None:
        if not self._validator_loop_alive():
            return "validator loop thread exited"
        if self._tick_stale():
            return "validator tick stale"
        return None

    def _start_api(self) -> None:
        port = int(self.config.endure.api_port)
        if port <= 0:
            return
        import threading

        import uvicorn

        from endure.api.app import build_app

        app = build_app(
            storage=self._storage,
            schema_id=self._schema_id,
            publisher=self._vertical_runtime.publisher,
            active_coordinates=self._vertical_runtime.active_coordinates,
            runtime_health=self.runtime_health,
            publication_identity=PublicationIdentity(
                signer=lambda payload: self.wallet.hotkey.sign(data=payload),
                hotkey=str(self.wallet.hotkey.ss58_address),
            ),
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=str(self.config.endure.api_host),
                port=port,
                log_level="warning",
            )
        )
        self._api_server = server
        self._api_thread = threading.Thread(target=server.run, daemon=True)
        self._api_thread.start()
        bt.logging.info(
            f"read API serving on "
            f"{safe_endpoint_label(f'{self.config.endure.api_host}:{port}')}"
        )

    def stop_run_thread(self) -> None:
        """Signal the read-API server to exit alongside the round loop, so a
        shutdown doesn't leave the uvicorn thread bound to the port."""
        self.should_exit = True
        self._shutdown_event.set()
        if self._api_server is not None:
            self._api_server.should_exit = True
        failures: list[Exception] = []
        try:
            super().stop_run_thread()
        except Exception as error:  # noqa: BLE001 - every worker still gets joined.
            failures.append(error)
        api_thread = self._api_thread
        if api_thread is not None:
            try:
                join_thread_or_raise(api_thread, name="validator read API")
            except RuntimeError as error:
                failures.append(error)
            else:
                self._api_thread = None
                self._api_server = None
        if failures:
            raise RuntimeError("validator shutdown incomplete") from failures[0]

    def close_transport_resources(self) -> None:
        failures: list[Exception] = []
        try:
            self._storage.close()
        except Exception as error:  # noqa: BLE001 - close every resource.
            failures.append(error)
        try:
            super().close_transport_resources()
        except Exception as error:  # noqa: BLE001 - preserve every cleanup failure.
            failures.append(error)
        if failures:
            raise RuntimeError("validator resource cleanup incomplete") from failures[0]

    def _build_service(self) -> ValidatorRoundService:
        if self._schema_id == RISK_SCHEMA_ID:
            runtime = _build_risk_vertical_runtime(self)
        elif self._schema_id == FORGE_LENDING_SCHEMA_ID:
            runtime = _build_forge_vertical_runtime(self)
        else:
            raise RuntimeError(
                f"no vertical runtime builder registered for schema {self._schema_id!r}"
            )
        self._vertical_runtime = runtime
        round_program = runtime.round_program
        entry = default_registry().get(self._schema_id)
        if runtime.publisher == "risk" and compression_enabled(self.config):
            universe_provider = StaticAlphaRiskUniverseProvider(
                netuids=_RECORDED_FIXTURE_NETUIDS,
                max_targets=len(_RECORDED_FIXTURE_NETUIDS),
            )
        else:
            universe_provider = entry.universe_provider
            if universe_provider is None:
                raise RuntimeError(
                    f"schema {self._schema_id!r} registry entry is missing "
                    "universe_provider"
                )
        return ValidatorRoundService(
            storage=self._storage,
            scheduler=runtime.scheduler,
            universe_provider=universe_provider,
            schema_id=self._schema_id,
            horizons=round_program.horizons,
            now_fn=_utc_now,
            max_universe_targets=entry.max_universe_targets,
            round_program=round_program,
            budget_factory=self._new_tick_budget,
        )

    def _blacklist(self, synapse: bt.Synapse) -> Tuple[bool, str]:
        dendrite = synapse.dendrite
        # Cross the SDK boundary through str so Decimal comparisons do not
        # inherit binary-float artifacts at the floor.
        return miner_admission(
            None if dendrite is None else dendrite.hotkey,
            registered_hotkeys=self.metagraph.hotkeys,
            stake_weight=lambda uid: Decimal(str(self.metagraph.S[uid])),
            min_stake=self.config.endure.min_miner_stake,
        )

    def _observed_emission_mode(self) -> str:
        """Read local policy facts only; never consult RPC from the health route."""
        if self.config.neuron.disable_set_weights:
            return "disabled"
        mode = select_emission_mode(
            getattr(self, "scores", ()),
            owner_vote_network=owner_vote_network(self.config),
        )
        if mode != "abstain" and getattr(self, "_emission_block", None) is not None:
            return "abstain"
        return mode

    @contextlib.contextmanager
    def _emission_state(self) -> Iterator[None]:
        """Hold the emission-state lock; log transitions only after release.

        A stalled log sink must never hold off /health, so messages recorded
        under the lock are emitted once the outermost holder releases it.
        """
        self._emission_state_lock.acquire()
        self._emission_lock_depth = getattr(self, "_emission_lock_depth", 0) + 1
        backlog: list[str] = []
        try:
            yield
        finally:
            self._emission_lock_depth -= 1
            if self._emission_lock_depth == 0:
                backlog = getattr(self, "_emission_log_backlog", [])
                self._emission_log_backlog = []
            self._emission_state_lock.release()
        for message in backlog:
            bt.logging.info(message)

    def _set_emission_observation(self, mode: str, reason: str) -> None:
        with self._emission_state():
            if (mode, reason) != (
                getattr(self, "_emission_mode", None),
                getattr(self, "_emission_reason", None),
            ):
                backlog: list[str] = getattr(self, "_emission_log_backlog", [])
                backlog.append(f"weight emission mode={mode} reason={reason}")
                self._emission_log_backlog = backlog
            self._emission_mode = mode
            self._emission_reason = reason

    def _defer_emission(self, reason: str) -> None:
        with self._emission_state():
            self._emission_expected_since = None
            self._emission_deadline = None
            mode = self._observed_emission_mode()
            if mode == "disabled" or (
                mode == "abstain" and getattr(self, "_emission_block", None) is None
            ):
                self._emission_blocked_reason = None
            self._set_emission_observation(mode, reason)

    def _emission_block_degraded(self) -> bool:
        """Blocked emission lets weights age toward activity_cutoff; page early."""
        block = getattr(self, "_emission_block", None)
        if block is None or self.config.neuron.disable_set_weights:
            return False
        if block in _IMMEDIATE_EMISSION_BLOCKS:
            return True
        # Transient reasons page only once the condition has been re-observed
        # for two epochs; a stale first observation alone never pages.
        since = getattr(self, "_emission_block_since_block", None)
        seen = getattr(self, "_emission_block_seen_block", None)
        return (
            since is not None
            and seen is not None
            and seen - since
            >= _TRANSIENT_EMISSION_BLOCK_EPOCHS * int(self.config.neuron.epoch_length)
        )

    def _note_head_block(self, block: int | None) -> None:
        """Remember the newest live head an emission decision was taken at."""
        if block is None:
            return
        with self._emission_state():
            known = getattr(self, "_emission_head_block", None)
            if known is None or block > known:
                self._emission_head_block = block

    def _refresh_emission_health(
        self, current_block: int | None, *, open_confirmation: bool
    ) -> None:
        with self._emission_state():
            # The cached metagraph block can trail the live head the plan's
            # chain due block came from by up to an epoch; judging a due
            # attempt against it would report chain_rate_limit and reset the
            # overdue clock.
            head = getattr(self, "_emission_head_block", None)
            if head is not None and (current_block is None or head > current_block):
                current_block = head
            mode = self._observed_emission_mode()
            if mode != getattr(self, "_emission_mode", None):
                self._emission_blocked_reason = None
                self._emission_expected_since = None
                self._emission_deadline = None
            self._emission_next_eligible_block = None
            reason = self._emission_wait_reason(mode, current_block, open_confirmation)
            if reason is not None:
                self._defer_emission(reason)
                return
            now = time.monotonic()
            if getattr(self, "_emission_expected_since", None) is None:
                self._emission_expected_since = now
                self._emission_deadline = max(
                    now + int(self.config.endure.health_tick_max_duration_seconds),
                    self._started_monotonic
                    + int(self.config.endure.health_startup_grace_seconds),
                )
            reason = getattr(self, "_emission_blocked_reason", None) or (
                "ready" if self.rpc_gate.ready() else "rpc_deferred"
            )
            if (
                reason == "ready"
                and self._emission_deadline is not None
                and now > self._emission_deadline
            ):
                reason = "submission_overdue"
            self._set_emission_observation(mode, reason)

    def _emission_wait_reason(  # noqa: PLR0911 — explicit, ordered eligibility gates.
        self, mode: str, block: int | None, open_confirmation: bool
    ) -> str | None:
        if mode == "disabled":
            return "disabled"
        if mode == "abstain":
            return getattr(self, "_emission_block", None) or "no_positive_scores"
        if open_confirmation:
            return "confirmation_pending"
        if block is None:
            return "chain_state_unavailable"
        storage = getattr(self, "_storage", None)
        if str(self.config.runtime.mode) != "mock":
            fence = (
                storage.weight_emission_startup_fence(
                    schema_id=self._schema_id, protocol_version_key=CURRENT_VERSION_KEY
                )
                if storage is not None
                else None
            )
            if fence is None or block <= fence:
                self._emission_next_eligible_block = (
                    None if fence is None else fence + 1
                )
                return "startup_fence"
        hotkeys = self.metagraph.hotkeys
        if not 0 <= int(self.uid) < len(hotkeys) or hotkeys[int(self.uid)] != str(
            self.wallet.hotkey.ss58_address
        ):
            return "validator_identity_invalid"
        uid = int(self.uid)
        permits = self.metagraph.validator_permit
        if uid < 0 or uid >= len(permits):
            return "chain_state_unavailable"
        permit = bool(permits[uid])
        if getattr(self, "_emission_snapshot_block", -1) >= block:
            permit = self._emission_snapshot_permit
        if not permit:
            return "no_validator_permit"
        chain_due = getattr(self, "_emission_chain_due_block", None)
        if chain_due is not None and block < chain_due:
            self._emission_next_eligible_block = chain_due
            return "chain_rate_limit"
        last_attempt = getattr(self, "_last_weights_attempt", None)
        if last_attempt is None:
            return "epoch_pacing"
        due = last_attempt + int(self.config.neuron.epoch_length) + 1
        # Once due, an unsuccessful attempt is not progress. Do not perpetually
        # renew its deadline simply because the scheduler paces another retry.
        if block < due and getattr(self, "_emission_expected_since", None) is None:
            self._emission_next_eligible_block = due
            return "epoch_pacing"
        return None

    def should_set_weights(self) -> bool:
        # The base constructor's first sync runs before durable scores exist;
        # planning or reporting a mode then would describe zero scores.
        if not getattr(self, "_durable_scores_loaded", False):
            return False
        due = super().should_set_weights()
        if due:
            # The base just paced this attempt on the live head (TTL-cached).
            self._note_head_block(self._safe_block())
        storage = getattr(self, "_storage", None)
        self._refresh_emission_health(
            _cached_block_number(vars(self.metagraph).get("block")),
            open_confirmation=(
                storage is not None
                and storage.has_open_weight_emission_confirmation(
                    schema_id=self._schema_id
                )
            ),
        )
        return due

    def set_weights(self) -> None:
        """Emit earned weights, or the owner vote whenever no score is positive.

        The score vector is rebuilt from durable EMAs first, so a restart, a
        failed tick or a metagraph resync never reads as zero scores. The
        owner allocation never enters scores or EMAs.
        """
        if self.config.neuron.disable_set_weights:
            self._defer_emission("disabled")
            return
        if not self._refresh_scores_from_durable_state():
            return
        network = owner_vote_network(self.config)
        mode = select_emission_mode(self.scores, owner_vote_network=network)
        if mode == "abstain":
            self._clear_emission_block()
            self._defer_emission("no_positive_scores")
            return
        self._set_emission_observation(
            self._observed_emission_mode(),
            getattr(self, "_emission_reason", "initializing"),
        )
        storage = getattr(self, "_storage", None)
        if not self._weight_emission_ready(storage):
            return
        plan = self._plan_emission(mode, network)
        if plan is None:
            return
        with self._emission_state():
            self._emission_blocked_reason = None
        self._owner_vote_recipient = plan.recipient
        try:
            self._emit_weight_candidate(plan.weights)
        except EmissionBlocked as blocked:
            # The prepared-vector recheck refused before sending; the emitter
            # already counted the failed attempt. Returning lets sync() advance
            # the attempt block, so the retry waits for the next epoch.
            bt.logging.warning(f"weight emission refused: {blocked.reason}: {blocked}")
            with self._emission_state():
                self._emission_blocked_reason = blocked.reason
                self._set_emission_observation(
                    self._observed_emission_mode(), blocked.reason
                )
        finally:
            self._owner_vote_recipient = None

    def _refresh_scores_from_durable_state(self) -> bool:
        try:
            self._reconstruct_scores()
        except Exception as error:  # noqa: BLE001 — never emit from stale state
            # A zeroed or stale vector must not read as owner_vote or scored;
            # abstain visibly and escalate like any other persistent block.
            self._block_emission(
                EmissionBlocked(
                    "score_state_unavailable",
                    f"durable score state unavailable: {safe_error(error)}",
                ),
                self._chain_block_hint(),
            )
            return False
        if getattr(self, "_emission_block", None) == "score_state_unavailable":
            with self._emission_state():
                underlying = getattr(self, "_emission_block_underlying", None)
                if underlying is None:
                    self._clear_emission_block()
                else:
                    # Only the score-read component resolved; the interrupted
                    # fault's streak (and its escalation clock) continues.
                    self._emission_block = underlying
                    self._emission_blocked_reason = underlying
                    self._emission_block_underlying = None
        return True

    def _chain_block_hint(self) -> int | None:
        block = self._safe_block()
        if block is None:
            block = _cached_block_number(vars(self.metagraph).get("block"))
        return block

    def _block_emission(self, blocked: EmissionBlocked, block: int | None) -> None:
        bt.logging.warning(f"weight emission abstains: {blocked.reason}: {blocked}")
        with self._emission_state():
            # One clock per continuous blocked streak, whatever the reason: a
            # reason flapping between snapshot faults must still page. Severity
            # is timed across observations, so a condition that clears before
            # the next attempt is never re-observed and never pages.
            if getattr(self, "_emission_block_since_block", None) is None:
                self._emission_block_since_block = block
            current = getattr(self, "_emission_block", None)
            if blocked.reason == "score_state_unavailable" and current not in {
                None,
                "score_state_unavailable",
            }:
                # Remember the fault the score-read failure interrupted.
                self._emission_block_underlying = current
            self._emission_block_seen_block = block
            self._emission_block = blocked.reason
            self._emission_blocked_reason = blocked.reason
            self._defer_emission(blocked.reason)

    def _clear_emission_block(self) -> None:
        """End the blocked streak: the condition resolved at an attempt/resync."""
        with self._emission_state():
            self._emission_block = None
            self._emission_block_since_block = None
            self._emission_block_seen_block = None
            self._emission_block_underlying = None

    def _weight_emission_ready(self, storage: Storage | None) -> bool:
        """Keep startup fencing and durable single-flight common to both modes."""
        if str(self.config.runtime.mode) != "mock":
            startup_fence = (
                storage.weight_emission_startup_fence(
                    schema_id=self._schema_id,
                    protocol_version_key=CURRENT_VERSION_KEY,
                )
                if storage is not None
                else getattr(self, "_weight_emission_startup_fence_block", 0)
            )
            if startup_fence is None:
                current_block = self._safe_block()
                if current_block is None:
                    self._defer_emission("chain_state_unavailable")
                    return False
                netuid = int(self.config.netuid)
                startup_fence = (
                    self.gated_subtensor.cr4_reveal_deadline_at(
                        netuid=netuid,
                        block=current_block,
                        finality_margin_blocks=WEIGHT_EMISSION_FINALITY_MARGIN_BLOCKS,
                    )
                    if self.gated_subtensor.commit_reveal_enabled(netuid=netuid)
                    else current_block
                    + WEIGHT_EMISSION_PERIOD_BLOCKS
                    + WEIGHT_EMISSION_FINALITY_MARGIN_BLOCKS
                )
                self._weight_emission_startup_fence_block = startup_fence
                if storage is not None:
                    storage.record_weight_emission_startup_fence(
                        schema_id=self._schema_id,
                        protocol_version_key=CURRENT_VERSION_KEY,
                        fence_block=startup_fence,
                    )
                self._defer_emission("startup_fence")
                return False
            if startup_fence > 0:
                current_block = self._safe_block()
                if current_block is None or current_block <= startup_fence:
                    self._defer_emission("startup_fence")
                    return False
        if storage is not None and storage.has_open_weight_emission_confirmation(
            schema_id=self._schema_id
        ):
            self._defer_emission("confirmation_pending")
            return False
        return True

    def _plan_emission(
        self,
        mode: Literal["scored", "owner_vote"],
        network: OwnerVoteNetwork | None,
    ) -> EmissionPlan | None:
        """Plan identity, permit, strict rate limit and recipient in one snapshot."""
        netuid = int(self.config.netuid)
        block = int(self.subtensor.get_current_block())
        self._note_head_block(block)
        info = self.subtensor.get_metagraph_info(
            netuid=netuid,
            selected_indices=list(CHAIN_SNAPSHOT_METAGRAPH_INDICES),
            block=block,
        )
        snapshot = (
            None
            if info is None
            else ChainSnapshot(
                block=info.block,
                hotkeys=info.hotkeys,
                owner_hotkey=info.owner_hotkey,
                validator_permit=info.validator_permit,
                last_update=info.last_update,
                weights_rate_limit=info.weights_rate_limit,
            )
        )
        try:
            plan = plan_emission(
                mode=mode,
                network=network,
                snapshot=snapshot,
                block=block,
                chain_identity=self.gated_subtensor.get_block_hash(0),
                netuid=netuid,
                validator_uid=int(self.uid),
                validator_hotkey=str(self.wallet.hotkey.ss58_address),
                local_hotkeys=self.metagraph.hotkeys,
                scores=self.scores,
            )
        except EmissionBlocked as blocked:
            self._block_emission(blocked, block)
            return None
        with self._emission_state():
            self._clear_emission_block()
            self._emission_chain_due_block = plan.next_eligible_block
            self._emission_snapshot_permit = plan.permit
            self._emission_snapshot_block = block
        if not plan.due:
            self._defer_emission(
                "no_validator_permit" if not plan.permit else "chain_rate_limit"
            )
            return None
        return plan

    def _emission_blended_snapshot(self) -> dict[str, Decimal]:
        cached: dict[str, Decimal] = getattr(self, "_blended_snapshot", {})
        if cached:
            return cached
        service = getattr(self, "_service", None)
        if service is None:
            return {}
        try:
            return service.blended_snapshot()
        except Exception:  # noqa: BLE001 — missing provenance must not block the audit write
            return {}

    def _emission_rows(self, attempt: WeightEmissionAttempt) -> list[WeightEmissionRow]:
        storage = getattr(self, "_storage", None)
        if storage is None:
            return []
        blended = (
            {}
            if getattr(self, "_owner_vote_recipient", None) is not None
            else self._emission_blended_snapshot()
        )
        u16_by_uid = dict(zip(attempt.uint_uids, attempt.uint_weights, strict=True))
        rows: list[WeightEmissionRow] = []
        for uid, processed in zip(
            attempt.processed_uids, attempt.processed_weights, strict=True
        ):
            hotkey = attempt.hotkeys[uid] if uid < len(attempt.hotkeys) else ""
            score = blended.get(hotkey)
            precap = (
                attempt.raw_weights[uid]
                if score is not None and uid < len(attempt.raw_weights)
                else None
            )
            u16 = u16_by_uid.get(uid)
            rows.append(
                WeightEmissionRow(
                    miner_hotkey=hotkey,
                    uid=uid,
                    blended_score=score,
                    weight_norm_precap=precap,
                    weight_processed=processed,
                    weight_u16=u16,
                    emitted=False,
                )
            )
        return rows

    def _on_weights_prepared(self, attempt: WeightEmissionAttempt) -> int | None:
        recipient: OwnerVoteRecipient | None = getattr(
            self, "_owner_vote_recipient", None
        )
        if recipient is not None:
            # Pre-submission recheck against the exact metagraph, chain
            # identity and constraints that produced this prepared vector.
            try:
                recheck_owner_vote(
                    recipient,
                    chain_identity=attempt.chain_identity or "",
                    netuid=attempt.netuid if attempt.netuid is not None else -1,
                    hotkeys=attempt.hotkeys,
                    uint_uids=attempt.uint_uids,
                    uint_weights=attempt.uint_weights,
                    min_allowed_weights=attempt.min_allowed_weights,
                    max_weight_limit=attempt.max_weight_limit,
                )
            except EmissionBlocked:
                self._record_refused_weight_attempt(attempt)
                raise
        self._set_emission_observation(self._observed_emission_mode(), "prepared")
        storage = getattr(self, "_storage", None)
        if storage is None:
            return None
        return self._record_emission_batch(
            storage,
            attempt,
            status="error",
            confirmation_state="prepared",
            confirmation_deadline_block=attempt.confirmation_deadline_block,
        )

    def _record_refused_weight_attempt(self, attempt: WeightEmissionAttempt) -> None:
        """Leave a durable failed record of a vector the recheck refused to send.

        The refusal must survive a restart in the emission history and in
        ``failed_weight_submissions_total``; no submission exists, so no
        confirmation deadline does either.
        """
        storage = getattr(self, "_storage", None)
        if storage is None:
            return
        try:
            self._record_emission_batch(
                storage,
                attempt,
                status="failed",
                confirmation_state="failed",
                confirmation_deadline_block=None,
            )
        except Exception as error:  # noqa: BLE001 — the refusal itself must still surface
            bt.logging.error(
                "could not record the refused weight attempt: "
                f"{type(error).__name__}: {safe_error(error)}"
            )

    def _record_emission_batch(
        self,
        storage: Storage,
        attempt: WeightEmissionAttempt,
        *,
        status: str,
        confirmation_state: str | None,
        confirmation_deadline_block: int | None,
    ) -> int:
        return storage.record_weight_emission(
            schema_id=self._schema_id,
            round_id=None,
            emitted_at_iso=_utc_now().isoformat(),
            block=attempt.block,
            min_allowed_weights=attempt.min_allowed_weights,
            max_weight_limit=attempt.max_weight_limit,
            metagraph_size=len(attempt.hotkeys),
            status=status,
            rows=self._emission_rows(attempt),
            submission_block=attempt.submission_block,
            confirmation_state=confirmation_state,
            baseline_last_update_block=attempt.baseline_last_update_block,
            period_blocks=attempt.period_blocks,
            chain_identity=attempt.chain_identity,
            netuid=attempt.netuid,
            validator_uid=attempt.validator_uid,
            validator_hotkey=attempt.validator_hotkey,
            submission_mode=attempt.submission_mode,
            intent_hash=attempt.intent_hash,
            protocol_version_key=attempt.protocol_version_key,
            commitment_hash=attempt.commitment_hash,
            reveal_round=attempt.reveal_round,
            confirmation_deadline_block=confirmation_deadline_block,
            cr4_reveal_deadline_block=attempt.cr4_reveal_deadline_block,
        )

    def _on_weights_emitted(
        self, attempt: WeightEmissionAttempt, batch_id: int | None = None
    ) -> None:
        if attempt.status == "submitted":
            self._defer_emission("confirmation_pending")
        else:
            with self._emission_state():
                reason = (
                    getattr(self, "_emission_blocked_reason", None)
                    or "submission_failed"
                )
                self._emission_blocked_reason = reason
                self._set_emission_observation(self._observed_emission_mode(), reason)
        storage = getattr(self, "_storage", None)
        if storage is None:
            return
        if batch_id is not None:
            confirmation_state = attempt.confirmation_state
            if confirmation_state is None:
                raise RuntimeError("prepared emission completion has no state")
            storage.transition_weight_emission_attempt(
                batch_id=batch_id,
                status=attempt.status,
                confirmation_state=confirmation_state,
                submission_mode=attempt.submission_mode or "direct",
                commitment_hash=attempt.commitment_hash,
                reveal_round=attempt.reveal_round,
                confirmation_deadline_block=attempt.confirmation_deadline_block,
                cr4_reveal_deadline_block=attempt.cr4_reveal_deadline_block,
            )
            return
        confirmation_state = attempt.confirmation_state
        if confirmation_state is None and attempt.status != "submitted":
            confirmation_state = "failed"
        self._record_emission_batch(
            storage,
            attempt,
            status=attempt.status,
            confirmation_state=confirmation_state,
            confirmation_deadline_block=attempt.confirmation_deadline_block,
        )

    def _on_metagraph_synced(self) -> None:
        if getattr(self, "_durable_scores_loaded", False):
            # Resync alignment has just zeroed UIDs whose hotkey changed. Rebuild
            # from durable EMAs before any confirmation RPC, so /health never
            # reads the zeroed vector as owner_vote and a miner that
            # re-registered at a new UID keeps its earned weight.
            self._refresh_scores_from_durable_state()
        self._resolve_weight_confirmations()

    def _resolve_weight_confirmations(self) -> None:
        """Resolve every restart-surviving submitted weight batch from chain state."""
        storage = getattr(self, "_storage", None)
        if storage is None:
            return
        finalized_block = self.gated_subtensor.finalized_block()
        finalized_hotkeys = self.gated_subtensor.hotkeys_at(
            netuid=int(self.config.netuid), block=finalized_block
        )
        hotkey_by_uid = dict(finalized_hotkeys)
        last_updates = self.gated_subtensor.last_updates_at(
            netuid=int(self.config.netuid), block=finalized_block
        )
        if not 0 <= self.uid < len(last_updates):
            bt.logging.error(f"validator uid {self.uid} is outside finalized state")
            return
        finalized_validator_hotkey = hotkey_by_uid.get(self.uid)
        if finalized_validator_hotkey != self.wallet.hotkey.ss58_address:
            bt.logging.error("finalized validator uid does not match the wallet hotkey")
            return
        all_weights = self.gated_subtensor.weights_at(
            netuid=int(self.config.netuid), block=finalized_block
        )
        validator_weights = next(
            (weights for uid, weights in all_weights if uid == self.uid), ()
        )
        commitments = tuple(
            WeightCommitEvidence(
                validator_hotkey=hotkey,
                commit_block=commit_block,
                commitment_hash=normalize_commitment_hash(commitment),
                reveal_round=reveal_round,
            )
            for hotkey, commit_block, commitment, reveal_round in (
                self.gated_subtensor.timelocked_weight_commits_at(
                    netuid=int(self.config.netuid), block=finalized_block
                )
            )
        )
        chain_identity = self.gated_subtensor.get_block_hash(0)
        confirmed = 0
        scanned_blocks = 0
        last_scanned_block: int | None = None
        scan_budget = _cr4_reveal_scan_batch_budget(
            int(self.config.neuron.epoch_length)
        )
        for _batch_number in range(scan_budget):
            scan_window = storage.cr4_reveal_scan_window(
                schema_id=self._schema_id, finalized_block=finalized_block
            )
            reveal_blocks = (
                self.gated_subtensor.timelocked_weight_reveals_between(
                    netuid=int(self.config.netuid),
                    validator_hotkey=str(finalized_validator_hotkey),
                    start_block=scan_window.start_block,
                    end_block=scan_window.end_block,
                )
                if scan_window is not None
                else ()
            )
            snapshot = WeightEmissionChainSnapshot(
                chain_identity=chain_identity,
                netuid=int(self.config.netuid),
                validator_uid=int(self.uid),
                validator_hotkey=str(finalized_validator_hotkey),
                block=finalized_block,
                last_update_block=last_updates[self.uid],
                weights=validator_weights,
                commitments=commitments,
                reveals=tuple(
                    WeightRevealEvidence(
                        validator_hotkey=str(finalized_validator_hotkey),
                        netuid=int(self.config.netuid),
                        reveal_block=block,
                    )
                    for block in reveal_blocks
                ),
                hotkeys=finalized_hotkeys,
                reveal_scan_complete=scan_window is None or scan_window.complete,
            )
            resolution = storage.resolve_weight_emission_confirmations(
                schema_id=self._schema_id,
                snapshot=snapshot,
                finality_margin_blocks=WEIGHT_EMISSION_FINALITY_MARGIN_BLOCKS,
                confirmed_at_iso=_utc_now().isoformat(),
            )
            confirmed += resolution.confirmed
            if scan_window is None:
                break
            storage.advance_cr4_reveal_scan_cursor(
                batch_id=scan_window.batch_id,
                scanned_through=scan_window.end_block,
            )
            scanned_blocks += scan_window.end_block - scan_window.start_block + 1
            last_scanned_block = scan_window.end_block
            if resolution.confirmed or resolution.unconfirmed:
                break
        if scanned_blocks and last_scanned_block is not None:
            bt.logging.info(
                f"CR4 reveal scan advanced {scanned_blocks} finalized block(s) "
                f"through block {last_scanned_block}"
            )
        if confirmed == 0:
            return
        self._consecutive_set_weights_failures = 0
        self._last_set_weights_ok = _utc_now().isoformat()
        with self._emission_state():
            self._emission_blocked_reason = None
            self._defer_emission("confirmed")
        bt.logging.info(
            f"confirmed {confirmed} weight emission batch(es) "
            f"at finalized block {finalized_block}"
        )

    def _attach_handlers(self) -> None:
        axon = getattr(self, "axon", None)
        if axon is None:
            bt.logging.warning("axon off — submission handlers not attached")
            return

        async def submit_commit(
            synapse: SubmitCommit, request: Request
        ) -> SubmitCommit:
            hotkey = authenticated_hotkey(request, synapse)
            return await self._handlers.handle_commit(synapse, miner_hotkey=hotkey)

        async def submit_commit_blacklist(
            synapse: SubmitCommit,
        ) -> Tuple[bool, str]:
            return self._blacklist(synapse)

        async def submit_reveal(
            synapse: SubmitReveal, request: Request
        ) -> SubmitReveal:
            hotkey = authenticated_hotkey(request, synapse)
            return await self._handlers.handle_reveal(synapse, miner_hotkey=hotkey)

        async def submit_reveal_blacklist(
            synapse: SubmitReveal,
        ) -> Tuple[bool, str]:
            return self._blacklist(synapse)

        axon.attach(
            forward_fn=submit_commit, blacklist_fn=submit_commit_blacklist
        ).attach(forward_fn=submit_reveal, blacklist_fn=submit_reveal_blacklist)
        # Registration publishes the axon; start its server only for live
        # runtimes. Mock mode attaches handlers without opening a socket.
        if str(self.config.runtime.mode) == "mock":
            bt.logging.info("mock runtime — handlers attached, server not started")
        else:
            axon.start()
            bt.logging.info("commit/reveal handlers attached; axon started")

    def _apply_weights(self, weights: dict[str, Decimal]) -> None:
        self.scores = [weights.get(hotkey, ZERO) for hotkey in self.metagraph.hotkeys]
        bt.logging.info(
            f"scores refreshed from blended EMAs ({len(weights)} miners scored)"
        )

    def _reconstruct_scores(self) -> None:
        weights = self._vertical_runtime.round_program.weights()
        self._blended_snapshot = self._vertical_runtime.round_program.blended_scores()
        self._apply_weights(weights)

    def resync_metagraph(self):
        """Advance the deregistration tracker once per metagraph refresh."""
        super().resync_metagraph()
        self._deregistration_tracker().advance(self.metagraph.hotkeys)

    def _deregistration_tracker(self) -> DeregistrationTracker:
        # The base constructor's first sync can resync before __init__ seeds.
        tracker: DeregistrationTracker | None = getattr(self, "_dereg_tracker", None)
        if tracker is None:
            tracker = DeregistrationTracker()
            self._dereg_tracker = tracker
        return tracker

    def _seed_deregistration_tracker(self) -> None:
        persisted = {
            state.miner_hotkey
            for state in self._storage.assessment_ema_states(self._schema_id)
        }
        self._deregistration_tracker().seed(self.metagraph.hotkeys, persisted)

    def _prune_archived_deregistrations(self) -> None:
        tracker = self._deregistration_tracker()
        if not tracker.confirmed():
            return
        storage = getattr(self, "_storage", None)
        if storage is None:
            return
        tracker.forget_settled(
            active_hotkeys={
                state.miner_hotkey
                for state in storage.assessment_ema_states(self._schema_id)
            },
            has_unfinished_submission=lambda hotkey: (
                storage.has_unfinished_assessment_submission(self._schema_id, hotkey)
            ),
        )

    async def forward(self) -> None:
        """One round-service tick; updates scores when new resolutions land."""
        self._begin_long_op()
        try:
            selected = scoring_set(
                self.metagraph.hotkeys, self._deregistration_tracker()
            )
            weights = await asyncio.to_thread(
                self._service.tick,
                expected_miners=list(selected.expected_miners),
                archive_hotkeys=list(selected.archive_hotkeys),
            )
            if weights is not None:
                self._blended_snapshot = self._service.blended_snapshot()
                self._apply_weights(weights)
            self._prune_archived_deregistrations()
            self._tick_failures = 0
            self._last_tick_ok = _utc_now().isoformat()
        except Exception as error:  # noqa: BLE001 — keep the loop alive
            self._tick_failures += 1
            # /health exposes only the error type; the local log carries the
            # message with endpoint URLs redacted via safe_error.
            self._last_tick_error = type(error).__name__
            bt.logging.error(
                f"validator tick failed ({self._tick_failures} consecutive): "
                f"{safe_error(error)}"
            )
        finally:
            self._end_long_op()
            # Heartbeat means the loop completed an attempt, not that external
            # work succeeded. Failure counters degrade /health separately;
            # only an unresponsive loop should trigger a forced restart.
            self._last_tick_monotonic = time.monotonic()
            # Throttle every tick — success and failure alike — to the configured
            # cadence. tick() is wall-clock gated, so spinning faster resolves no
            # rounds sooner and just burns CPU/disk (each step re-saves state).
            await asyncio.to_thread(
                self._shutdown_event.wait,
                int(self.config.endure.tick_seconds),
            )


def _recorded_fixture_block(_reveal_close: datetime) -> int:
    return RECORDED_FIXTURE_WINDOW_START_BLOCK


def _build_risk_vertical_runtime(validator: Validator) -> VerticalRuntime:
    from endure.assessment.schemas.subnet_alpha_risk import RiskSubmissionBundle

    if compression_enabled(validator.config):
        require_compression_runtime_allowed(validator.config)
        risk_runtime = build_risk_devnet_runtime(validator.config, now=_utc_now())
        scheduler = risk_runtime.scheduler
        price_provider = risk_runtime.price_provider
        due_seconds = risk_runtime.due_seconds_by_horizon
        reveal_close_block = _recorded_fixture_block
        window_end_block = None
    else:
        scheduler = scheduler_for_schema(RISK_SCHEMA_ID)
        if permits_dev_only_runtime(validator.config):
            price_provider = recorded_mainnet_fixture_provider()
            reveal_close_block = _recorded_fixture_block
            window_end_block = None
        else:
            live_provider = LiveAlphaPriceProvider(
                config=LiveAlphaPriceProviderConfig(
                    endpoint=str(validator.config.endure.market_data_endpoint)
                ),
                progress_fn=validator._mark_tick_progress,
                deadline_exceeded_fn=validator._tick_budget_exhausted,
            )

            def live_reveal_close_block(reveal_close: datetime) -> int:
                return live_provider.block_for_reveal_close(
                    reveal_close, now=_utc_now()
                )

            def live_window_end_block(window_end: datetime) -> int:
                return live_provider.last_finalized_block_at_or_before(
                    window_end, now=_utc_now()
                )

            reveal_close_block = live_reveal_close_block
            window_end_block = live_window_end_block
            price_provider = live_provider
        due_seconds = {}
    orchestrator = RiskScoringOrchestrator(
        storage=validator._storage,
        price_provider=price_provider,
        half_life_rounds=DEFAULT_PAYOUT_HALF_LIFE_ROUNDS,
        reveal_close_block=reveal_close_block,
        window_end_block=window_end_block,
        registered_hotkeys=lambda: list(validator.metagraph.hotkeys),
        **(
            {"active_netuids": _RECORDED_FIXTURE_NETUIDS}
            if compression_enabled(validator.config)
            else {}
        ),
    )
    return VerticalRuntime(
        round_program=AssessmentRoundProgram(
            storage=validator._storage,
            schema_id=RISK_SCHEMA_ID,
            bundle_model=RiskSubmissionBundle,
            orchestrator=orchestrator,
            horizons=RISK_HORIZONS,
            due_seconds_by_horizon=due_seconds,
        ),
        publisher="risk",
        scheduler=scheduler,
        active_coordinates=orchestrator.active_coordinates,
    )


def _build_forge_vertical_runtime(validator: Validator) -> VerticalRuntime:
    from endure.assessment.schemas.forge_lending import (
        LENDING_HORIZON_SECONDS,
        LendingSubmissionBundle,
    )
    from endure.scoring.lending.orchestrator import LendingScoringOrchestrator

    scheduler = scheduler_for_schema(FORGE_LENDING_SCHEMA_ID)
    orchestrator = LendingScoringOrchestrator(
        storage=validator._storage,
        price_provider=recorded_mainnet_fixture_provider(),
        half_life_rounds=DEFAULT_PAYOUT_HALF_LIFE_ROUNDS,
    )
    return VerticalRuntime(
        round_program=AssessmentRoundProgram(
            storage=validator._storage,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            bundle_model=LendingSubmissionBundle,
            orchestrator=orchestrator,
            horizons=(LENDING_HORIZON_SECONDS,),
            due_seconds_by_horizon={},
        ),
        publisher="assessment",
        scheduler=scheduler,
    )


def _force_restart_if_rpc_abandoned(validator: Validator) -> None:
    if validator.chain_rpc_restart_required() is not True:
        return
    bt.logging.error(
        "validator forcing process restart after chain RPC "
        "abandonment capacity was reached"
    )
    # Drain the log queues first: the abandoned non-daemon RPC workers would
    # hang a normal interpreter shutdown, and a raw os._exit loses this line.
    terminate_process(1, grace_seconds=_WATCHDOG_TEARDOWN_GRACE_SECONDS)


_WATCHDOG_TEARDOWN_GRACE_SECONDS = 60
# Within Docker's 45 s stop grace: a signal during construction waits this long
# for construction to finish before the startup guard ends the process.
_STARTUP_SHUTDOWN_GRACE_SECONDS = 10


def _schedule_forced_exit_after_grace() -> threading.Timer:
    # SystemExit only reaches the finalization-free entrypoint boundary after
    # `with validator` teardown joins its workers — and a wedged tick worker may
    # never return. A daemon timer bounds that teardown while it still runs
    # with threads alive.
    timer = threading.Timer(_WATCHDOG_TEARDOWN_GRACE_SECONDS, os._exit, args=(1,))
    timer.daemon = True
    timer.start()
    return timer


def main() -> None:
    try:
        configure_log_shipping("endure-validator")
        identity = runtime_identity()
        bt.logging.info(
            "runtime identity "
            f"source_revision={identity['source_revision']} "
            f"image_version={identity['image_version']} "
            f"protocol_version_key={CURRENT_VERSION_KEY}"
        )
        stop = install_shutdown_handlers()
        startup = StartupShutdownGuard(
            stop, grace_seconds=_STARTUP_SHUTDOWN_GRACE_SECONDS
        )
        validator = Validator()
        startup.started()
        try:
            with validator:
                while not stop.is_set():
                    _force_restart_if_rpc_abandoned(validator)
                    if (reason := validator.watchdog_exit_reason()) is not None:
                        # The worker may have died by latching between the check
                        # above and this liveness probe; a plain SystemExit here
                        # would take the normal exit the latch exists to prevent.
                        _force_restart_if_rpc_abandoned(validator)
                        bt.logging.error(f"validator watchdog exiting: {reason}")
                        _schedule_forced_exit_after_grace()
                        raise SystemExit(1)
                    bt.logging.info(f"Validator running... {time.time()}")
                    stop.wait(5)
                # A shutdown signal that races the latch must not fall through
                # to the normal exit the latch exists to prevent.
                _force_restart_if_rpc_abandoned(validator)
        finally:
            # The RPC worker can also latch while __exit__ joins it — and
            # __exit__ itself raises on incomplete cleanup, so this recheck
            # must run on the exception path too, not only after a clean exit.
            _force_restart_if_rpc_abandoned(validator)
        bt.logging.info("validator stopped on shutdown signal")
    except DevOnlyConfigError as error:
        bt.logging.error(f"validator refused to start: {safe_error(error)}")
        raise SystemExit(1) from None
    except Exception as error:  # noqa: BLE001 - CLI boundary must redact SDK errors.
        bt.logging.error(
            f"validator failed: {type(error).__name__}: {safe_error(error)}"
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    # Construction can exit (sys.exit for an unregistered hotkey) or fail after
    # abandoning a non-daemon archive worker; the boundary never finalizes.
    run_entrypoint(main, grace_seconds=_WATCHDOG_TEARDOWN_GRACE_SECONDS)
