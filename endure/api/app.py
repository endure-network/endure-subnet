"""Validator read API enforcing the Alpha Risk V1 consensus embargo.

Public, read-only. Submissions and consensus for a round are never serveable
while it is open: round-scoped endpoints (scores, consensus, submissions,
outcomes) pass through the central ``_ensure_embargo_lifted`` gate, which 403s
until the round is in a known post-embargo state. A new endpoint that aggregates
across rounds instead has no single round to gate and must apply the same
post-embargo allowlist at the repository read path. Risk and economic values are
serialized as Decimal strings; only operational telemetry (timing and rate
fields) is serialized as a plain number.

The API is unauthenticated by design (public, read-only). It does no rate
limiting itself; a soak/production deployment MUST place it behind an external
rate limiter / reverse proxy. List endpoints are bounded and paginated so a
single response can't be made arbitrarily large.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import TYPE_CHECKING, Final, NotRequired, TypedDict

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Gauge,
    generate_latest,
)
from prometheus_client.core import CounterMetricFamily

from endure import __version__
from endure.api import RoundResolutionHealth, assessment_round_resolution_health
from endure.assessment.coordinates import (
    AssessmentConsensusRow,
    AssessmentCoordinate,
    AssessmentEmaState,
    AssessmentRealizedTarget,
    AssessmentScoreHistoryRow,
)
from endure.assessment.registry import SchemaRegistryEntry, default_registry
from endure.assessment.schemas.subnet_alpha_risk import RISK_HORIZONS
from endure.protocol.version_contract import CURRENT_VERSION_KEY
from endure.publication.risk_feed import Signer, build_signed_risk_feed
from endure.runtime.identity import runtime_identity
from endure.scoring.context import TR_CONTEXT
from endure.scoring.weights import normalize_weights
from endure.storage.repository import (
    POST_EMBARGO_ROUND_STATES,
    ROUND_STATE_OPEN,
    Storage,
)

if TYPE_CHECKING:
    from endure.protocol.vertical import PublisherProjection

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200
# /health returns the unfinished-round COUNT plus this many sample ids — over a
# long soak the full list is unbounded and would bloat every probe.
_HEALTH_ROUNDS_SAMPLE = 10
# Policy B: one empty scored round can be a genuine quiet day, so /health only
# degrades once this many consecutive scored rounds carry zero submissions.
_EMPTY_SCORED_ROUNDS_HEALTH_THRESHOLD = 2
# A horizon coming due is resolved by the first budgeted tick after the due
# boundary, which can legitimately take minutes of archive work; with zero
# grace the 2026-09-09 00:08Z soak probe read a healthy midnight catch-up as
# degraded/503. One worst-case tick must elapse past due before "overdue"
# means a missed window. The validator supplies its configured
# health_tick_max_duration_seconds via runtime_health; this constant is only
# the fallback for runtimes that do not report one (mock/dev builds).
_OVERDUE_GRACE_SECONDS = 1800
_RUNTIME_COUNTER_KEYS = (
    "consecutive_universe_failures",
    "consecutive_resolution_failures",
    "consecutive_empty_scored_rounds",
)
logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RpcGateHealth(TypedDict):
    adaptive_rate: float
    degraded: bool
    rate_limited_total: int
    deferred_total: int
    abandoned_generations: int


class RuntimeHealth(TypedDict):
    """Validator-loop health surfaced under /health's ``runtime`` field.

    A dead loop, stale tick, nonzero tick/universe/resolution failures, or two
    consecutive empty scored rounds returns a degraded 503 response.
    """

    process_started_at: NotRequired[str]
    process_uptime_seconds: NotRequired[int]
    validator_loop_alive: bool
    tick_stale: bool
    seconds_since_last_tick: float | None
    long_op_in_flight: NotRequired[bool]
    seconds_since_long_op_start: NotRequired[float | None]
    consecutive_tick_failures: int
    last_tick_ok: str | None
    last_tick_error: str | None
    consecutive_universe_failures: NotRequired[int]
    last_universe_error: str | None
    consecutive_resolution_failures: NotRequired[int]
    last_resolution_error: str | None
    consecutive_empty_scored_rounds: NotRequired[int]
    last_empty_scored_round: str | None
    last_set_weights_ok: NotRequired[str | None]
    consecutive_set_weights_failures: NotRequired[int]
    weight_emission_degraded: NotRequired[bool]
    emission_mode: NotRequired[str]
    emission_reason: NotRequired[str]
    emission_blocked_reason: NotRequired[str | None]
    emission_expected: NotRequired[bool]
    emission_next_eligible_block: NotRequired[int | None]
    emission_expected_seconds: NotRequired[float | None]
    emission_deadline_in_seconds: NotRequired[float | None]
    emission_submission_overdue: NotRequired[bool]
    emission_confirmation_deadline_block: NotRequired[int | None]
    last_confirmed_weights_at: NotRequired[str | None]
    open_weight_submissions: NotRequired[int]
    oldest_open_weight_submission_age_blocks: NotRequired[int | None]
    latest_unconfirmed_weight_submission_block: NotRequired[int | None]
    failed_weight_submissions_total: NotRequired[int]
    rpc_rate_limited_process_total: NotRequired[int]
    rpc_deferred_process_total: NotRequired[int]
    weight_submissions_failed_process_total: NotRequired[int]
    rpc_gate: NotRequired[RpcGateHealth]
    assessment_due_seconds: NotRequired[dict[int, int]]
    overdue_grace_seconds: NotRequired[int]


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    runtime: RuntimeHealth | None
    unfinished_round_count: int
    unfinished_rounds: tuple[str, ...]
    round_resolution: RoundResolutionHealth | None
    degraded: bool


@dataclass(frozen=True, slots=True)
class _CounterMetric:
    name: str
    documentation: str
    value: int


@dataclass(frozen=True, slots=True)
class _CounterProjection:
    metrics: tuple[_CounterMetric, ...]

    def collect(self) -> tuple[CounterMetricFamily, ...]:
        return tuple(
            CounterMetricFamily(metric.name, metric.documentation, value=metric.value)
            for metric in self.metrics
        )


@dataclass(frozen=True, slots=True)
class PublicationIdentity:
    signer: Signer | None
    hotkey: str | None


_UNSIGNED_PUBLICATION_IDENTITY: Final = PublicationIdentity(None, None)


def _bounded(limit: int) -> int:
    return max(1, min(limit, _MAX_LIMIT))


def _schema_parameter_names(schema: object) -> list[str]:
    names: list[str] = []
    for spec in getattr(schema, "parameters", ()):
        name = getattr(spec, "name", None)
        if isinstance(name, str):
            names.append(name)
            continue
        output = getattr(spec, "output", None)
        output_value = getattr(output, "value", None)
        names.append(output_value if isinstance(output_value, str) else str(output))
    return names


def _schema_summary(entry: SchemaRegistryEntry) -> dict[str, object]:
    schema = entry.schema
    schema_id = getattr(schema, "schema_id", None)
    if not isinstance(schema_id, str):
        raise TypeError("registered schema must expose schema_id")
    context = getattr(schema, "context", None)
    benchmark = getattr(context, "benchmark", None)
    if benchmark is None:
        benchmark = getattr(context, "context_id", None)
    return {
        "schema_id": schema_id,
        "serving_status": entry.serving_status,
        "benchmark": benchmark,
        "horizons_seconds": list(entry.horizons_seconds),
        "parameters": _schema_parameter_names(schema),
    }


def _mean(values: list[Decimal]) -> Decimal:
    with localcontext(TR_CONTEXT):
        return sum(values, Decimal(0)) / Decimal(len(values))


def _assessment_leaderboard(
    storage: Storage,
    schema_id: str,
    active_coordinates: frozenset[AssessmentCoordinate] | None,
) -> list[dict[str, object]]:
    states = (
        state
        for state in storage.assessment_ema_states(schema_id)
        if active_coordinates is None or state.coordinate in active_coordinates
    )
    by_hotkey: dict[str, list[AssessmentEmaState]] = {}
    for state in states:
        by_hotkey.setdefault(state.miner_hotkey, []).append(state)
    blends = {
        hotkey: _mean([state.ema for state in hotkey_states])
        for hotkey, hotkey_states in sorted(by_hotkey.items())
        if hotkey_states
    }
    weights = normalize_weights(blends)
    ranked = sorted(blends, key=lambda hotkey: (-blends[hotkey], hotkey))
    return [
        {
            "rank": position,
            "miner_hotkey": hotkey,
            "blended_score": str(blends[hotkey]),
            "weight_share": str(weights.get(hotkey, Decimal(0))),
            "horizon_emas": _assessment_horizon_emas(by_hotkey[hotkey]),
            "coordinate_emas": _assessment_ema_payloads(by_hotkey[hotkey]),
        }
        for position, hotkey in enumerate(ranked, start=1)
    ]


def _assessment_horizon_emas(states: list[AssessmentEmaState]) -> dict[str, str]:
    grouped: dict[int, list[Decimal]] = {}
    for state in states:
        grouped.setdefault(state.coordinate.horizon_value, []).append(state.ema)
    return {str(horizon): str(_mean(emas)) for horizon, emas in sorted(grouped.items())}


def _assessment_ema_payloads(
    states: list[AssessmentEmaState],
) -> list[dict[str, object]]:
    return [
        {
            "target_kind": state.coordinate.target_kind,
            "target_id": state.coordinate.target_id,
            "horizon_seconds": state.coordinate.horizon_value,
            "output": state.coordinate.output,
            "ema": str(state.ema),
            "resolved_rounds": state.resolved_rounds,
        }
        for state in sorted(states, key=lambda item: item.coordinate)
    ]


def _assessment_consensus_payload(row: AssessmentConsensusRow) -> dict[str, object]:
    return {
        "target_kind": row.coordinate.target_kind,
        "target_id": row.coordinate.target_id,
        "horizon_seconds": row.coordinate.horizon_value,
        "output": row.coordinate.output,
        "median": str(row.value),
        "mad": str(row.dispersion),
        "n_submitters": row.n_submitters,
    }


def _assessment_score_payload(row: AssessmentScoreHistoryRow) -> dict[str, object]:
    return {
        "miner_hotkey": row.miner_hotkey,
        "target_kind": row.coordinate.target_kind,
        "target_id": row.coordinate.target_id,
        "horizon_seconds": row.coordinate.horizon_value,
        "output": row.coordinate.output,
        "round_score": str(row.round_score),
        "ema_after": str(row.ema_after),
    }


def _assessment_target_payload(row: AssessmentRealizedTarget) -> dict[str, object]:
    return {
        "target_kind": row.coordinate.target_kind,
        "target_id": row.coordinate.target_id,
        "horizon_seconds": row.coordinate.horizon_value,
        "output": row.coordinate.output,
        "value": None if row.value is None else str(row.value),
        "status": row.status,
        "provider_payload_hash": row.provider_payload_hash,
    }


def _universe_payload(
    storage: Storage, schema_id: str, round_id: str
) -> dict[str, object]:
    snapshot = storage.universe_for(round_id, schema_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="round has no universe")
    return {
        "round_id": round_id,
        "tickers": list(snapshot.tickers),
        "source_hash": snapshot.source_hash,
    }


def _round_meta_or_404(
    storage: Storage, schema_id: str, round_id: str
) -> dict[str, object]:
    meta = storage.round_meta(round_id, schema_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="unknown round")
    return meta


def _embargo_lifted(meta: dict[str, object]) -> bool:
    if meta["state"] not in POST_EMBARGO_ROUND_STATES:
        return False
    available_raw = meta.get("publication_available_at")
    if not isinstance(available_raw, str):
        return False
    try:
        available_at = datetime.fromisoformat(available_raw)
    except ValueError:
        return False
    return available_at.tzinfo is not None and _utc_now() > available_at


def _ensure_embargo_lifted(meta: dict[str, object]) -> None:
    state = meta["state"]
    if state in POST_EMBARGO_ROUND_STATES:
        if _embargo_lifted(meta):
            return
        raise HTTPException(
            status_code=403,
            detail="round data remains embargoed until the next commit window closes",
        )
    if state == ROUND_STATE_OPEN:
        raise HTTPException(
            status_code=403,
            detail="round is still open — submissions and consensus are "
            "embargoed until the reveal window closes",
        )
    raise HTTPException(
        status_code=403,
        detail="round is not in a readable post-embargo state",
    )


def _health_snapshot(
    storage: Storage,
    schema_id: str,
    publisher: PublisherProjection,
    /,
    *,
    runtime_health: Callable[[], RuntimeHealth] | None,
) -> HealthSnapshot:
    unfinished_rounds = tuple(storage.unfinished_rounds(schema_id))
    runtime = None if runtime_health is None else runtime_health()
    round_resolution = None
    degraded = False
    if publisher == "risk":
        round_resolution = assessment_round_resolution_health(
            storage.unfinished_assessment_resolution_progress(schema_id),
            RISK_HORIZONS,
            now=_utc_now(),
            sample_limit=_HEALTH_ROUNDS_SAMPLE,
            due_seconds=(
                None if runtime is None else runtime.get("assessment_due_seconds")
            ),
            overdue_grace_seconds=(
                _OVERDUE_GRACE_SECONDS
                if runtime is None
                else runtime.get("overdue_grace_seconds", _OVERDUE_GRACE_SECONDS)
            ),
        )
        degraded = round_resolution["overdue_round_count"] > 0
    if runtime is not None:
        missing_counter_keys = [
            key for key in _RUNTIME_COUNTER_KEYS if key not in runtime
        ]
        if missing_counter_keys:
            logger.warning(
                "runtime health payload omitted counters: %s",
                ", ".join(missing_counter_keys),
            )
        rpc_gate = runtime.get("rpc_gate")
        rpc_degraded = rpc_gate is not None and rpc_gate["degraded"]
        degraded = degraded or (
            not runtime["validator_loop_alive"]
            or runtime["tick_stale"]
            or runtime["consecutive_tick_failures"] > 0
            or runtime.get("consecutive_universe_failures", 0) > 0
            or runtime.get("consecutive_resolution_failures", 0) > 0
            or runtime.get("consecutive_empty_scored_rounds", 0)
            >= _EMPTY_SCORED_ROUNDS_HEALTH_THRESHOLD
            or runtime.get("consecutive_set_weights_failures", 0) > 0
            or runtime.get("weight_emission_degraded", False)
            or rpc_degraded
        )
    return HealthSnapshot(
        runtime=runtime,
        unfinished_round_count=len(unfinished_rounds),
        unfinished_rounds=unfinished_rounds,
        round_resolution=round_resolution,
        degraded=degraded,
    )


def _metrics_response(snapshot: HealthSnapshot) -> Response:
    metrics: list[tuple[str, str, int | float]] = [
        ("endure_validator_live", "Whether the API process is serving.", 1),
        (
            "endure_validator_ready",
            "Whether the validator health snapshot is not degraded.",
            int(not snapshot.degraded),
        ),
        (
            "endure_validator_unfinished_rounds",
            "Current count of non-terminal rounds.",
            snapshot.unfinished_round_count,
        ),
    ]
    if snapshot.round_resolution is not None:
        metrics.extend(
            [
                (
                    "endure_validator_pending_rounds",
                    "Current rounds awaiting realized targets.",
                    snapshot.round_resolution["pending_round_count"],
                ),
                (
                    "endure_validator_overdue_rounds",
                    "Current rounds past the resolution deadline.",
                    snapshot.round_resolution["overdue_round_count"],
                ),
            ]
        )
    if snapshot.runtime is not None:
        runtime = snapshot.runtime
        metrics.extend(
            [
                (
                    "endure_validator_loop_alive",
                    "Whether the validator loop is alive.",
                    int(runtime["validator_loop_alive"]),
                ),
                (
                    "endure_validator_tick_stale",
                    "Whether the latest completed tick is beyond its freshness window.",
                    int(runtime["tick_stale"]),
                ),
                (
                    "endure_validator_tick_failures_consecutive",
                    "Current consecutive validator tick failures.",
                    runtime["consecutive_tick_failures"],
                ),
            ]
        )
        tick_age = runtime["seconds_since_last_tick"]
        if tick_age is not None:
            metrics.append(
                (
                    "endure_validator_tick_age_seconds",
                    "Age of the latest completed validator tick in seconds.",
                    tick_age,
                )
            )
        optional_counts = (
            (
                "consecutive_resolution_failures",
                "endure_validator_resolution_failures_consecutive",
                "Current consecutive resolution failures.",
            ),
            (
                "consecutive_universe_failures",
                "endure_validator_universe_failures_consecutive",
                "Current consecutive universe-opening failures.",
            ),
            (
                "consecutive_empty_scored_rounds",
                "endure_validator_empty_scored_rounds_consecutive",
                "Current consecutive empty scored rounds.",
            ),
            (
                "consecutive_set_weights_failures",
                "endure_validator_set_weights_failures_consecutive",
                "Current consecutive set-weights failures.",
            ),
            (
                "open_weight_submissions",
                "endure_validator_weight_submissions_open",
                "Current weight submissions awaiting confirmation.",
            ),
            (
                "oldest_open_weight_submission_age_blocks",
                "endure_validator_weight_submissions_oldest_open_age_blocks",
                "Age of the oldest open weight submission in blocks.",
            ),
            (
                "latest_unconfirmed_weight_submission_block",
                "endure_validator_weight_submissions_latest_unconfirmed_block",
                "Latest unconfirmed weight-submission block height.",
            ),
        )
        for runtime_key, metric_name, documentation in optional_counts:
            value = runtime.get(runtime_key)
            if type(value) is int:
                metrics.append((metric_name, documentation, value))
        weight_emission_degraded = runtime.get("weight_emission_degraded")
        if isinstance(weight_emission_degraded, bool):
            metrics.append(
                (
                    "endure_validator_weight_emission_degraded",
                    "Whether weight-emission confirmation is degraded.",
                    int(weight_emission_degraded),
                )
            )
        confirmed_timestamp: datetime | None = None
        confirmed_at = runtime.get("last_confirmed_weights_at")
        if isinstance(confirmed_at, str):
            try:
                confirmed_timestamp = datetime.fromisoformat(confirmed_at)
            except (OSError, OverflowError, ValueError):
                confirmed_timestamp = None
        if confirmed_timestamp is not None and confirmed_timestamp.tzinfo is not None:
            metrics.append(
                (
                    "endure_validator_weights_last_confirmed_timestamp_seconds",
                    "Last confirmed on-chain weight-emission time as Unix seconds.",
                    confirmed_timestamp.timestamp(),
                )
            )
        rpc_gate = runtime.get("rpc_gate")
        rpc_degraded = (
            rpc_gate.get("degraded") if isinstance(rpc_gate, Mapping) else None
        )
        if isinstance(rpc_degraded, bool):
            metrics.append(
                (
                    "endure_validator_rpc_degraded",
                    "Whether the RPC gate is currently degraded.",
                    int(rpc_degraded),
                )
            )
    registry = CollectorRegistry()
    for name, documentation, value in metrics:
        Gauge(name, documentation, registry=registry).set(value)
    if snapshot.runtime is not None:
        counter_metrics = (
            (
                "endure_validator_rpc_rate_limited",
                "Count of provider rate-limit responses observed by the validator process.",
                snapshot.runtime.get("rpc_rate_limited_process_total"),
            ),
            (
                "endure_validator_rpc_deferred",
                "Count of chain-RPC operations deferred during provider cooldown.",
                snapshot.runtime.get("rpc_deferred_process_total"),
            ),
            (
                "endure_validator_weight_submissions_failed",
                "Count of explicit unsuccessful weight-submission responses.",
                snapshot.runtime.get("weight_submissions_failed_process_total"),
            ),
        )
        registry.register(
            _CounterProjection(
                tuple(
                    _CounterMetric(name, documentation, value)
                    for name, documentation, value in counter_metrics
                    if type(value) is int
                )
            )
        )
    return Response(
        content=generate_latest(registry),
        headers={"Content-Type": CONTENT_TYPE_LATEST},
    )


def _register_core_routes(  # noqa: PLR0913 — explicit read API dependencies
    app: FastAPI,
    storage: Storage,
    schema_id: str,
    publisher: PublisherProjection,
    /,
    *,
    runtime_health: Callable[[], RuntimeHealth] | None,
    active_coordinates: frozenset[AssessmentCoordinate] | None,
) -> None:
    @app.get("/live")
    async def live() -> dict[str, str]:
        # Runs on the event loop, not the 40-token worker pool shared with
        # /health and the public read endpoints: blocked /health requests
        # can never starve the container healthcheck. Keep it trivial: no
        # database, no lock, no await on anything that can block.
        return {"status": "live"}

    @app.get("/health")
    def health(response: Response) -> dict[str, object]:
        snapshot = _health_snapshot(
            storage,
            schema_id,
            publisher,
            runtime_health=runtime_health,
        )
        payload: dict[str, object] = {
            "status": "ok",
            "schema_id": schema_id,
            "version": __version__,
            "protocol_version_key": CURRENT_VERSION_KEY,
            **runtime_identity(),
            "unfinished_round_count": snapshot.unfinished_round_count,
            "unfinished_rounds": snapshot.unfinished_rounds[:_HEALTH_ROUNDS_SAMPLE],
        }
        if snapshot.round_resolution is not None:
            payload["round_resolution"] = snapshot.round_resolution
        if snapshot.runtime is not None:
            payload["runtime"] = snapshot.runtime
        if snapshot.degraded:
            payload["status"] = "degraded"
            response.status_code = 503
        return payload

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return _metrics_response(
            _health_snapshot(
                storage,
                schema_id,
                publisher,
                runtime_health=runtime_health,
            )
        )

    @app.get("/schemas")
    def schemas() -> list[dict[str, object]]:
        registry = default_registry()
        payload: list[dict[str, object]] = []
        for registered_id in registry.schema_ids():
            entry = registry.get(registered_id)
            payload.append(_schema_summary(entry))
        return payload

    @app.get("/rounds")
    def rounds_index(
        state: str | None = None,
        limit: int = _DEFAULT_LIMIT,
        before: str | None = None,
    ) -> list[dict[str, object]]:
        return storage.list_rounds(
            schema_id, state=state, limit=_bounded(limit), before=before
        )

    @app.get("/miners")
    def miners_leaderboard() -> list[dict[str, object]]:
        return _assessment_leaderboard(storage, schema_id, active_coordinates)

    @app.get("/rounds/{round_id}")
    def round_meta(round_id: str) -> dict[str, object]:
        meta = _round_meta_or_404(storage, schema_id, round_id)
        if not _embargo_lifted(meta):
            # Keep windows and frozen inputs readable for miners, but do not
            # disclose reveal participation while miners can still adapt their
            # decision to reveal or abort.
            meta = {**meta, "accepted_submissions": None}
        return meta

    @app.get("/rounds/{round_id}/universe")
    def universe(round_id: str) -> dict[str, object]:
        # Deliberately NOT embargoed: the frozen universe is round input
        # miners need during the commit window, not submission data.
        _round_meta_or_404(storage, schema_id, round_id)
        return _universe_payload(storage, schema_id, round_id)


def _register_round_data_routes(
    app: FastAPI,
    storage: Storage,
    schema_id: str,
) -> None:
    @app.get("/rounds/{round_id}/scores")
    def round_scores(round_id: str) -> list[dict[str, object]]:
        meta = _round_meta_or_404(storage, schema_id, round_id)
        _ensure_embargo_lifted(meta)
        return [
            _assessment_score_payload(row)
            for row in storage.assessment_score_history_for_round(round_id, schema_id)
        ]

    @app.get("/rounds/{round_id}/consensus")
    def consensus(round_id: str) -> list[dict[str, object]]:
        meta = _round_meta_or_404(storage, schema_id, round_id)
        _ensure_embargo_lifted(meta)
        return [
            _assessment_consensus_payload(row)
            for row in storage.assessment_consensus_for(round_id, schema_id)
        ]

    @app.get("/rounds/{round_id}/submissions")
    def submissions(
        round_id: str, limit: int = _DEFAULT_LIMIT, offset: int = 0
    ) -> dict[str, object]:
        meta = _round_meta_or_404(storage, schema_id, round_id)
        _ensure_embargo_lifted(meta)
        bounded = _bounded(limit)
        page_offset = max(0, offset)
        # Fetch one past the page to learn has_more without a count query — a
        # round can carry one accepted bundle per registered miner, so the full
        # set is bounded but must still be paged and capped.
        rows = storage.accepted_bundles(
            round_id, schema_id, limit=bounded + 1, offset=page_offset
        )
        return {
            "round_id": round_id,
            "limit": bounded,
            "offset": page_offset,
            "has_more": len(rows) > bounded,
            "submissions": [
                {"miner_hotkey": hotkey, "bundle": json.loads(bundle_json)}
                for hotkey, bundle_json in rows[:bounded]
            ],
        }

    @app.get("/rounds/{round_id}/outcomes")
    def outcomes(round_id: str) -> list[dict[str, object]]:
        meta = _round_meta_or_404(storage, schema_id, round_id)
        _ensure_embargo_lifted(meta)
        return [
            _assessment_target_payload(row)
            for row in storage.assessment_realized_targets_for(round_id, schema_id)
        ]

    @app.get("/miners/{hotkey}/scores")
    def miner_scores(hotkey: str) -> dict[str, object]:
        states = [
            state
            for state in storage.assessment_ema_states(schema_id)
            if state.miner_hotkey == hotkey
        ]
        if not states:
            raise HTTPException(status_code=404, detail="unknown miner")
        return {
            "miner_hotkey": hotkey,
            "emas": _assessment_ema_payloads(states),
        }


def _register_risk_routes(
    app: FastAPI,
    storage: Storage,
    publication_identity: PublicationIdentity,
) -> None:
    @app.get("/risk/v1/subnets")
    def risk_subnets() -> dict[str, object]:
        return build_signed_risk_feed(
            storage,
            signer=publication_identity.signer,
            signed_by=publication_identity.hotkey,
            now=_utc_now(),
        )


def build_app(  # noqa: PLR0913 — explicit read API dependencies
    *,
    storage: Storage,
    schema_id: str,
    publisher: PublisherProjection,
    runtime_health: Callable[[], RuntimeHealth] | None = None,
    active_coordinates: frozenset[AssessmentCoordinate] | None = None,
    publication_identity: PublicationIdentity = _UNSIGNED_PUBLICATION_IDENTITY,
) -> FastAPI:
    app = FastAPI(title="Endure Alpha Risk validator read API", version=__version__)
    # Read-only public API: permissive CORS so browser dashboards can call it.
    # allow_headers="*" so a preflight for custom request headers succeeds.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET"],
        allow_headers=["*"],
    )
    _register_core_routes(
        app,
        storage,
        schema_id,
        publisher,
        runtime_health=runtime_health,
        active_coordinates=active_coordinates,
    )
    _register_round_data_routes(app, storage, schema_id)
    _register_risk_routes(app, storage, publication_identity)
    return app
