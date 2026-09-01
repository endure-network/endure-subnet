"""Endure validator entrypoint for schema-routed risk assessment.

Alpha Risk (``risk.v1.subnet_alpha``) is the served vertical. Forge lending
remains a dormant reference vertical
selectable with ``--endure.active_schema``. The validator serves the
commit/reveal axon, drives the selected round service every tick (open → embargo
→ resolve → score → close), and replaces the score vector with blended miner
EMAs whenever scoring happens.
"""

import asyncio
import os
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, Tuple, runtime_checkable

import bittensor as bt

if TYPE_CHECKING:
    import threading

    import uvicorn
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig

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
from endure.base.shutdown import install_shutdown_handlers, join_thread_or_raise
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
)
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
    permits_dev_only_runtime,
    require_compression_runtime_allowed,
    require_explicit_netuid,
    require_serving_stage_allowed,
)
from endure.utils.logging import safe_endpoint_label, safe_error

_RECORDED_FIXTURE_NETUIDS: Final = (8, 44)
ZERO = Decimal("0")

DEREGISTRATION_CONFIRMATION_SYNCS = 2


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


class Validator(BaseValidatorNeuron):
    """Schema-routed validator round loop."""

    def __init__(self, config: bt.Config | None = None) -> None:
        resolved_config = config or type(self).build_config()
        require_serving_stage_allowed(resolved_config)
        require_explicit_netuid(resolved_config)
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
        super().__init__(
            config=resolved_config,
            runtime_provider=resolve_runtime_provider(resolved_config),
        )
        self._schema_id = active_runtime_schema_id(self.config)
        _run_migrations(self.config.endure.database_url)
        self._storage = Storage.from_url(self.config.endure.database_url)
        self._weight_emission_startup_fence_block: int | None = None
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
        self._seed_deregistration_tracker()
        self._tick_failures = 0
        self._last_tick_ok: str | None = None
        self._last_tick_monotonic: float | None = None
        self._last_tick_error: str | None = None
        self._started_monotonic = time.monotonic()
        self._api_server: uvicorn.Server | None = None
        self._api_thread: threading.Thread | None = None
        self._attach_handlers()
        self._start_api()
        # D1=B: the code default stays 0; the operator's deployment sets the
        # floor. Warn loudly when a live network runs with no stake gate — any
        # registered hotkey can then impose commit/reveal load.
        if str(self.config.runtime.mode) != "mock" and (
            self.config.endure.min_miner_stake <= 0
        ):
            bt.logging.warning(
                "endure.min_miner_stake is 0 on a live network — any registered "
                "hotkey can impose commit/reveal load; pass "
                "--endure.min_miner_stake with a positive TAO floor"
            )

    def runtime_health(self) -> RuntimeHealth:
        """Stuck-loop observability, merged into /health. Tick fields are the
        validator's; universe-fetch fields come from the round service (a
        failed open is swallowed there but must still surface as degraded)."""
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
        weight_emission_degraded = (
            gate.degraded
            or gate.abandoned_generations > 0
            or self._consecutive_set_weights_failures > 0
            or unresolved_unconfirmed
            or deadline_overdue
            or fallback_overdue
            or unknown_block_open
        )
        return {
            "validator_loop_alive": self._validator_loop_alive(),
            "tick_stale": self._tick_stale(),
            "seconds_since_last_tick": self._seconds_since_last_tick(),
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
            "last_set_weights_ok": self._last_set_weights_ok,
            "consecutive_set_weights_failures": self._consecutive_set_weights_failures,
            "weight_emission_degraded": weight_emission_degraded,
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

    def _mark_tick_progress(self) -> None:
        """Refresh tick liveness from bounded in-tick work, so a long catch-up
        tick survives the watchdog while a wedged thread still trips it."""
        self._last_tick_monotonic = time.monotonic()

    def _tick_stale(self) -> bool:
        now = time.monotonic()
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
        )

    def _blacklist(self, synapse: bt.Synapse) -> Tuple[bool, str]:
        if synapse.dendrite is None or synapse.dendrite.hotkey is None:
            return True, "Missing dendrite or hotkey"
        if synapse.dendrite.hotkey not in self.metagraph.hotkeys:
            return True, "Unrecognized hotkey"
        # Registration is cheap; the stake gate bounds who can impose
        # commit/reveal load. The threshold is parsed to Decimal at argparse
        # time (boot); the chain-native metagraph float is crossed into Decimal
        # through str to avoid binary-float artifacts at the threshold.
        min_stake = self.config.endure.min_miner_stake
        if min_stake > 0:
            uid = self.metagraph.hotkeys.index(synapse.dendrite.hotkey)
            if Decimal(str(self.metagraph.S[uid])) < min_stake:
                return True, "Insufficient stake"
        return False, "Hotkey recognized"

    def set_weights(self):
        """Abstain until something has scored: all-zero scores would emit the
        SDK's uniform fallback — pure noise into consensus during validator
        warm-up."""
        if not any(score != ZERO for score in self.scores):
            bt.logging.info("no resolved scores yet — abstaining from set_weights")
            return
        storage = getattr(self, "_storage", None)
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
                    return
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
                bt.logging.info("weight emission startup fence initialized")
                return
            if startup_fence > 0:
                current_block = self._safe_block()
                if current_block is None or current_block <= startup_fence:
                    return
        if storage is not None and storage.has_open_weight_emission_confirmation(
            schema_id=self._schema_id
        ):
            bt.logging.info(
                "weight emission remains unconfirmed — abstaining from a new submission"
            )
            return
        super().set_weights()

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
        blended = self._emission_blended_snapshot()
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
        storage = getattr(self, "_storage", None)
        if storage is None:
            return None
        return storage.record_weight_emission(
            schema_id=self._schema_id,
            round_id=None,
            emitted_at_iso=_utc_now().isoformat(),
            block=attempt.block,
            min_allowed_weights=attempt.min_allowed_weights,
            max_weight_limit=attempt.max_weight_limit,
            metagraph_size=len(attempt.hotkeys),
            status="error",
            rows=self._emission_rows(attempt),
            submission_block=attempt.submission_block,
            confirmation_state="prepared",
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
            confirmation_deadline_block=attempt.confirmation_deadline_block,
            cr4_reveal_deadline_block=attempt.cr4_reveal_deadline_block,
        )

    def _on_weights_emitted(
        self, attempt: WeightEmissionAttempt, batch_id: int | None = None
    ) -> None:
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
        storage.record_weight_emission(
            schema_id=self._schema_id,
            round_id=None,
            emitted_at_iso=_utc_now().isoformat(),
            block=attempt.block,
            min_allowed_weights=attempt.min_allowed_weights,
            max_weight_limit=attempt.max_weight_limit,
            metagraph_size=len(attempt.hotkeys),
            status=attempt.status,
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
            confirmation_deadline_block=attempt.confirmation_deadline_block,
            cr4_reveal_deadline_block=attempt.cr4_reveal_deadline_block,
        )

    def _on_metagraph_synced(self) -> None:
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
        bt.logging.info(
            f"confirmed {confirmed} weight emission batch(es) "
            f"at finalized block {finalized_block}"
        )

    def _attach_handlers(self) -> None:
        axon = getattr(self, "axon", None)
        if axon is None:
            bt.logging.warning("axon off — submission handlers not attached")
            return

        async def submit_commit(synapse: SubmitCommit) -> SubmitCommit:
            hotkey = synapse.dendrite.hotkey if synapse.dendrite else None
            if not hotkey:
                synapse.accepted = False
                return synapse
            return await self._handlers.handle_commit(synapse, miner_hotkey=hotkey)

        async def submit_commit_blacklist(
            synapse: SubmitCommit,
        ) -> Tuple[bool, str]:
            return self._blacklist(synapse)

        async def submit_reveal(synapse: SubmitReveal) -> SubmitReveal:
            hotkey = synapse.dendrite.hotkey if synapse.dendrite else None
            if not hotkey:
                synapse.accepted = False
                return synapse
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
        if weights:
            self._apply_weights(weights)

    def resync_metagraph(self):
        """Advance the deregistration tracker once per metagraph refresh.

        Fairness-deltas spec §1 decision 3: the two-sync confirmation counts
        metagraph resync generations — never scoring-pass ticks, which can
        repeat against one stale snapshot. The tracker is deliberately
        in-memory: a restart only delays archival by one confirmation cycle
        while the hotkey keeps receiving zero observations.
        """
        super().resync_metagraph()
        self._advance_deregistration_tracker(set(self.metagraph.hotkeys))

    def _seed_deregistration_tracker(self) -> None:
        """Baseline the tracker from durable EMA state, not process history.

        Fairness-deltas spec §1 decision 3 promises a restart merely delays
        archival by one confirmation cycle. Without this seed, a hotkey whose
        EMA state persisted while it was already absent from the first
        post-restart metagraph would never enter the missing-count tracker
        and could never reach two-sync archival.
        """
        persisted = {
            state.miner_hotkey
            for state in self._storage.assessment_ema_states(self._schema_id)
        }
        self._dereg_missing_counts: dict[str, int] = {}
        self._dereg_last_registered: set[str] = set(self.metagraph.hotkeys) | persisted

    def _advance_deregistration_tracker(self, current: set[str]) -> None:
        counts = getattr(self, "_dereg_missing_counts", None)
        if counts is None:
            counts = {}
            self._dereg_missing_counts = counts
        last_registered: set[str] = getattr(self, "_dereg_last_registered", set())
        for hotkey in (set(counts) | last_registered) - current:
            counts[hotkey] = counts.get(hotkey, 0) + 1
        for hotkey in current:
            counts.pop(hotkey, None)
        self._dereg_last_registered = current

    def _confirmed_deregistered(self) -> list[str]:
        counts: dict[str, int] = getattr(self, "_dereg_missing_counts", {})
        return sorted(
            hotkey
            for hotkey, missed in counts.items()
            if missed >= DEREGISTRATION_CONFIRMATION_SYNCS
        )

    def _prune_archived_deregistrations(self) -> None:
        counts: dict[str, int] = getattr(self, "_dereg_missing_counts", {})
        confirmed = self._confirmed_deregistered()
        if not confirmed:
            return
        storage = getattr(self, "_storage", None)
        if storage is None:
            return
        active_hotkeys = {
            state.miner_hotkey
            for state in storage.assessment_ema_states(self._schema_id)
        }
        for hotkey in confirmed:
            if (
                hotkey not in active_hotkeys
                and not storage.has_unfinished_assessment_submission(
                    self._schema_id, hotkey
                )
            ):
                counts.pop(hotkey, None)

    async def forward(self) -> None:
        """One round-service tick; updates scores when new resolutions land."""
        try:
            weights = await asyncio.to_thread(
                self._service.tick,
                expected_miners=list(self.metagraph.hotkeys),
                archive_hotkeys=self._confirmed_deregistered(),
            )
            if weights:
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
        scheduler = scheduler_for_schema(
            RISK_SCHEMA_ID,
            fetch_delay_seconds=int(validator.config.endure.fetch_delay_seconds),
        )
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
    )


def _build_forge_vertical_runtime(validator: Validator) -> VerticalRuntime:
    from endure.assessment.schemas.forge_lending import (
        LENDING_HORIZON_SECONDS,
        LendingSubmissionBundle,
    )
    from endure.scoring.lending.orchestrator import LendingScoringOrchestrator

    scheduler = scheduler_for_schema(
        FORGE_LENDING_SCHEMA_ID,
        fetch_delay_seconds=int(validator.config.endure.fetch_delay_seconds),
    )
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


def main() -> None:
    try:
        identity = runtime_identity()
        bt.logging.info(
            "runtime identity "
            f"source_revision={identity['source_revision']} "
            f"image_version={identity['image_version']} "
            f"protocol_version_key={CURRENT_VERSION_KEY}"
        )
        stop = install_shutdown_handlers()
        validator = Validator()
        with validator:
            while not stop.is_set():
                if validator.chain_rpc_restart_required() is True:
                    bt.logging.error(
                        "validator forcing process restart after chain RPC "
                        "abandonment capacity was reached"
                    )
                    os._exit(1)
                if (reason := validator.watchdog_exit_reason()) is not None:
                    bt.logging.error(f"validator watchdog exiting: {reason}")
                    raise SystemExit(1)
                bt.logging.info(f"Validator running... {time.time()}")
                stop.wait(5)
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
    main()
