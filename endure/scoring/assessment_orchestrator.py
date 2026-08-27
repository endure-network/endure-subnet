"""Schema-neutral assessment scoring orchestration (risk scope spec §R1)."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Final, Protocol, assert_never

from endure.assessment.coordinates import (
    AssessmentCoordinate,
    AssessmentEmaState,
    AssessmentOutputScore,
    AssessmentRealizedTarget,
    AssessmentScoreHistoryRow,
)
from endure.assessment.schemas.wire import AggressiveDirection, DeviationMode
from endure.scoring.context import TR_CONTEXT
from endure.scoring.weights import ema_update, normalize_weights
from endure.storage.repository import Storage

REALIZED_TARGET_RESOLVED = "resolved"
REALIZED_TARGET_VOIDED = "voided"

# Fairness-deltas spec §1 decision 6: an absent hotkey whose every scored
# coordinate EMA has decayed below this threshold is archived out of the
# expected scoring set. Under the cubic weight sharpening an EMA of 0.01
# maps to weight ∝ 1e-6 — negligible — and the archive stops the permanent
# zero-row churn in assessment_score_history.
ARCHIVE_EPSILON = Decimal("0.01")


class AssessmentScoringSpec(Protocol):
    @property
    def grace_band(self) -> int: ...

    @property
    def cutoff(self) -> int: ...

    @property
    def aggressive_direction(self) -> AggressiveDirection: ...

    @property
    def lenient_multiplier(self) -> Decimal: ...

    @property
    def deviation_mode(self) -> DeviationMode: ...

    @property
    def scored_live(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class AssessmentResolutionContext:
    window_start_block: int | None
    window_end_block: int | None
    void_unavailable_targets: bool
    force_void_unavailable_targets: bool


# Default for resolvers that need no window bounds (e.g. lending's
# full-history resolver); risk builds a real context per scoring pass.
NEUTRAL_RESOLUTION_CONTEXT: Final = AssessmentResolutionContext(
    window_start_block=None,
    window_end_block=None,
    void_unavailable_targets=False,
    force_void_unavailable_targets=False,
)


type AssessmentResolver = Callable[
    [AssessmentResolutionContext, int, int], AssessmentRealizedTarget | None
]


@dataclass(frozen=True, slots=True)
class ScoredOutputConfig:
    output: str
    resolver: AssessmentResolver
    spec: AssessmentScoringSpec


class SubmittedAssessmentOutput[TOutput](Protocol):
    @property
    def output(self) -> TOutput: ...

    @property
    def value(self) -> int: ...

    @property
    def horizon_seconds(self) -> int: ...


class SubmittedAssessmentAsset[TOutput](Protocol):
    @property
    def netuid(self) -> int: ...

    @property
    def outputs(self) -> Sequence[SubmittedAssessmentOutput[TOutput]]: ...


def _subnet_asset_coordinate(
    netuid: int, horizon: int, output: str
) -> AssessmentCoordinate:
    return AssessmentCoordinate.subnet_asset(
        netuid=netuid, horizon_seconds=horizon, output=output
    )


@dataclass(frozen=True, slots=True)
class AssessmentScoringConfig:
    schema_id: str
    horizons: tuple[int, ...]
    universe_members: Callable[[tuple[str, ...]], Iterable[int]]
    accepted_values: Callable[[str], Mapping[str, Mapping[AssessmentCoordinate, int]]]
    outputs: tuple[ScoredOutputConfig, ...]
    coordinate_for: Callable[[int, int, str], AssessmentCoordinate] = (
        _subnet_asset_coordinate
    )


def deviation_score(
    submitted: int, target: int, spec: AssessmentScoringSpec
) -> Decimal:
    """Asymmetric bounded-linear score in [0, 1]."""
    with localcontext(TR_CONTEXT):
        deviation = submitted - target
        match spec.deviation_mode:
            case DeviationMode.RELATIVE:
                if target == 0:
                    return Decimal(1) if deviation == 0 else Decimal(0)
                magnitude = (
                    abs(Decimal(deviation)) / abs(Decimal(target)) * Decimal(10000)
                )
            case DeviationMode.ABSOLUTE:
                magnitude = Decimal(abs(deviation))
            case unreachable:
                assert_never(unreachable)
        match spec.aggressive_direction:
            case AggressiveDirection.HIGHER:
                on_aggressive_side = deviation > 0
            case AggressiveDirection.LOWER:
                on_aggressive_side = deviation < 0
            case unreachable:
                assert_never(unreachable)
        effective_cutoff = Decimal(spec.cutoff)
        if not on_aggressive_side:
            effective_cutoff *= spec.lenient_multiplier
        if magnitude <= spec.grace_band:
            return Decimal(1)
        if magnitude >= effective_cutoff:
            return Decimal(0)
        decay_width = effective_cutoff - Decimal(spec.grace_band)
        return Decimal(1) - (magnitude - Decimal(spec.grace_band)) / decay_width


def score_output(submitted: int, target: int, spec: AssessmentScoringSpec) -> Decimal:
    if not spec.scored_live:
        raise NotImplementedError(
            "risk scope §R1 — resolver-table outputs must be scored-live to run"
        )
    return deviation_score(submitted, target, spec)


def submitted_assessment_values[TOutput](
    assets: Iterable[SubmittedAssessmentAsset[TOutput]],
    coordinate_for: Callable[[int, int, TOutput], AssessmentCoordinate | None],
) -> dict[AssessmentCoordinate, int]:
    values: dict[AssessmentCoordinate, int] = {}
    for asset in assets:
        for output_value in asset.outputs:
            coordinate = coordinate_for(
                asset.netuid, output_value.horizon_seconds, output_value.output
            )
            if coordinate is not None:
                values[coordinate] = output_value.value
    return values


def _fully_decayed_hotkeys(
    absent: set[str],
    *,
    previous: Mapping[tuple[str, AssessmentCoordinate], AssessmentEmaState],
    updates: Sequence[AssessmentEmaState],
    scored_outputs: set[str],
) -> set[str]:
    post_update: dict[str, dict[AssessmentCoordinate, Decimal]] = {
        hotkey: {} for hotkey in absent
    }
    for (hotkey, coordinate), state in previous.items():
        if hotkey in post_update and coordinate.output in scored_outputs:
            post_update[hotkey][coordinate] = state.ema
    for update in updates:
        per_hotkey = post_update.get(update.miner_hotkey)
        if per_hotkey is not None and update.coordinate.output in scored_outputs:
            per_hotkey[update.coordinate] = update.ema
    return {
        hotkey
        for hotkey, emas in post_update.items()
        if emas and max(emas.values()) < ARCHIVE_EPSILON
    }


class AssessmentScoringOrchestrator:
    """Resolve targets, score the expected scoring set, and maintain EMAs.

    Fairness-deltas spec §1: the scoring set is ``current submitters ∪
    (active-EMA hotkeys ∩ historically eligible hotkeys)`` — absence is a zero
    observation per resolved coordinate only after a miner's first accepted
    round. Skipping is never better than submitting a bad prediction, while a
    later joiner cannot be charged retroactively when long horizons resolve out
    of order. Registration state is deliberately NOT intersected into the
    scoring set; it is carried separately (``registered_hotkeys``) and used
    only for weight eligibility, so a hotkey missing from a single stale
    metagraph snapshot still receives its zero observation while archival
    waits for the two-sync confirmation upstream (``archive_hotkeys``).
    """

    def __init__(
        self,
        *,
        storage: Storage,
        config: AssessmentScoringConfig,
        half_life_rounds: int,
        registered_hotkeys: Callable[[], Sequence[str]] | None = None,
    ) -> None:
        self._storage = storage
        self._config = config
        self._half_life = half_life_rounds
        self._registered_hotkeys = registered_hotkeys

    @property
    def horizons(self) -> tuple[int, ...]:
        return self._config.horizons

    def resolve_and_score(  # noqa: PLR0913
        self,
        round_id: str,
        horizon: int | None = None,
        *,
        now_iso: str,
        resolution_due_at: datetime | None = None,
        archive_hotkeys: Sequence[str] = (),
        context: AssessmentResolutionContext = NEUTRAL_RESOLUTION_CONTEXT,
    ) -> dict[str, Decimal]:
        _ = resolution_due_at
        scoring_horizon = self._single_horizon() if horizon is None else horizon
        if self._storage.has_assessment_resolution_marker(
            round_id, self._config.schema_id, scoring_horizon
        ):
            return {}
        universe = self._storage.universe_for(round_id, self._config.schema_id)
        if universe is None:
            raise ValueError(f"assessment round {round_id} has no stored universe")
        netuids = sorted(self._config.universe_members(universe.tickers))
        expected_coordinates = frozenset(
            self._config.coordinate_for(netuid, scoring_horizon, output.output)
            for netuid in netuids
            for output in self._config.outputs
        )
        existing_targets = self._storage.assessment_realized_targets_for_horizon(
            round_id, self._config.schema_id, scoring_horizon
        )
        existing_coordinates = frozenset(
            target.coordinate for target in existing_targets
        )
        targets = self._resolve_targets(
            context, netuids, scoring_horizon, existing_coordinates
        )
        complete = expected_coordinates.issubset(
            existing_coordinates | {target.coordinate for target in targets}
        )
        resolved = {
            target.coordinate: target.value
            for target in targets
            if target.status == REALIZED_TARGET_RESOLVED and target.value is not None
        }
        accepted_values = self._config.accepted_values(round_id)
        output_by_name = {output.output: output for output in self._config.outputs}
        previous_emas = {
            (state.miner_hotkey, state.coordinate): state
            for state in self._storage.assessment_ema_states(self._config.schema_id)
        }
        previously_active = {hotkey for hotkey, _coordinate in previous_emas}
        historically_eligible = self._storage.assessment_hotkeys_eligible_for_round(
            self._config.schema_id, round_id
        )
        scoring_set = (previously_active & historically_eligible) | set(accepted_values)

        output_scores: list[AssessmentOutputScore] = []
        ema_updates: list[AssessmentEmaState] = []
        history: list[AssessmentScoreHistoryRow] = []
        round_scores: dict[str, Decimal] = {}
        for hotkey in sorted(scoring_set):
            coordinate_scores: list[Decimal] = []
            per_coordinate = accepted_values.get(hotkey, {})
            for coordinate, target_value in sorted(resolved.items()):
                output_config = output_by_name[coordinate.output]
                submitted = per_coordinate.get(coordinate)
                if submitted is None:
                    score = Decimal(0)
                    error: Decimal | None = None
                else:
                    score = score_output(
                        submitted, int(target_value), output_config.spec
                    )
                    error = Decimal(abs(submitted - int(target_value)))
                coordinate_scores.append(score)
                output_scores.append(
                    AssessmentOutputScore(
                        miner_hotkey=hotkey,
                        coordinate=coordinate,
                        score=score,
                        error=error,
                    )
                )
                prior = previous_emas.get((hotkey, coordinate))
                ema = ema_update(
                    prior.ema if prior is not None else None,
                    score,
                    half_life_rounds=self._half_life,
                )
                ema_updates.append(
                    AssessmentEmaState(
                        miner_hotkey=hotkey,
                        coordinate=coordinate,
                        ema=ema,
                        resolved_rounds=(prior.resolved_rounds if prior else 0) + 1,
                    )
                )
                history.append(
                    AssessmentScoreHistoryRow(
                        miner_hotkey=hotkey,
                        coordinate=coordinate,
                        round_score=score,
                        ema_after=ema,
                    )
                )
            if coordinate_scores:
                with localcontext(TR_CONTEXT):
                    round_scores[hotkey] = sum(coordinate_scores, Decimal(0)) / Decimal(
                        len(coordinate_scores)
                    )

        zero_filled_hotkeys = {update.miner_hotkey for update in ema_updates} - set(
            accepted_values
        )
        pruned = _fully_decayed_hotkeys(
            zero_filled_hotkeys,
            previous=previous_emas,
            updates=ema_updates,
            scored_outputs={output.output for output in self._config.outputs},
        )
        self._storage.record_assessment_scoring_pass(
            round_id,
            self._config.schema_id,
            horizon_value=scoring_horizon,
            realized_targets=targets,
            output_scores=output_scores,
            ema_updates=ema_updates,
            score_history=history,
            complete=complete,
            now_iso=now_iso,
            archive_hotkeys=archive_hotkeys,
            pruned_hotkeys=sorted(pruned),
        )
        return round_scores

    def blended_scores(self) -> dict[str, Decimal]:
        by_hotkey: dict[str, list[Decimal]] = {}
        scored_outputs = {output.output for output in self._config.outputs}
        for state in self._storage.assessment_ema_states(self._config.schema_id):
            if state.coordinate.output not in scored_outputs:
                continue
            by_hotkey.setdefault(state.miner_hotkey, []).append(state.ema)
        with localcontext(TR_CONTEXT):
            return {
                hotkey: sum(emas, Decimal(0)) / Decimal(len(emas))
                for hotkey, emas in sorted(by_hotkey.items())
            }

    def weights(self) -> dict[str, Decimal]:
        blended = self.blended_scores()
        if self._registered_hotkeys is not None:
            registered = set(self._registered_hotkeys())
            blended = {
                hotkey: score
                for hotkey, score in blended.items()
                if hotkey in registered
            }
        return normalize_weights(blended)

    def _single_horizon(self) -> int:
        [horizon] = self._config.horizons
        return horizon

    def _resolve_targets(
        self,
        context: AssessmentResolutionContext,
        netuids: Sequence[int],
        horizon: int,
        existing_coordinates: frozenset[AssessmentCoordinate],
    ) -> list[AssessmentRealizedTarget]:
        targets: list[AssessmentRealizedTarget] = []
        for netuid in netuids:
            for output in self._config.outputs:
                coordinate = self._config.coordinate_for(netuid, horizon, output.output)
                if coordinate in existing_coordinates:
                    continue
                target = output.resolver(context, netuid, horizon)
                if target is not None:
                    targets.append(target)
        return targets
