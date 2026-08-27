"""Generic assessment scoring orchestration (risk scope §Reused vs. net-new)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from endure.assessment.coordinates import (
    AssessmentCoordinate,
    AssessmentEmaState,
    AssessmentRealizedTarget,
)
from endure.assessment.lending_universe import StaticLendingUniverseProvider
from endure.assessment.schemas.forge_lending import (
    FORGE_LENDING_SCHEMA_ID,
    AggressiveDirection,
    DeviationMode,
)
from endure.protocol.round_engine import DEFAULT_OFFSETS, compute_windows
from endure.scoring.assessment_orchestrator import (
    AssessmentResolutionContext,
    AssessmentScoringConfig,
    AssessmentScoringOrchestrator,
    ScoredOutputConfig,
)
from endure.storage.repository import Storage

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC).isoformat()
ROUND = "2026-07-06"


@dataclass(frozen=True, slots=True)
class _Spec:
    grace_band: int
    cutoff: int
    aggressive_direction: AggressiveDirection
    lenient_multiplier: Decimal
    deviation_mode: DeviationMode
    scored_live: bool = True


def _open_round(storage: Storage) -> None:
    storage.open_round(
        windows=compute_windows(date(2026, 7, 6), offsets=DEFAULT_OFFSETS),
        schema_id=FORGE_LENDING_SCHEMA_ID,
        universe=StaticLendingUniverseProvider(netuids=(44,)).fetch_universe(ROUND),
        now_iso=NOW,
    )


def _coordinate(output: str, horizon: int) -> AssessmentCoordinate:
    return AssessmentCoordinate.subnet_asset(
        netuid=44, horizon_seconds=horizon, output=output
    )


def test_resolver_table_dispatches_multiple_outputs_generically(
    storage: Storage,
) -> None:
    _open_round(storage)

    def resolve_alpha(
        _context: AssessmentResolutionContext, netuid: int, horizon: int
    ) -> AssessmentRealizedTarget:
        return AssessmentRealizedTarget(
            coordinate=AssessmentCoordinate.subnet_asset(
                netuid=netuid, horizon_seconds=horizon, output="alpha"
            ),
            value=Decimal(1000),
            status="resolved",
        )

    def resolve_beta(
        _context: AssessmentResolutionContext, netuid: int, horizon: int
    ) -> AssessmentRealizedTarget:
        return AssessmentRealizedTarget(
            coordinate=AssessmentCoordinate.subnet_asset(
                netuid=netuid, horizon_seconds=horizon, output="beta"
            ),
            value=Decimal(2000),
            status="resolved",
        )

    def accepted_values(round_id: str) -> dict[str, dict[AssessmentCoordinate, int]]:
        assert round_id == ROUND
        return {
            "hk-a": {
                _coordinate("alpha", 5): 1000,
                _coordinate("beta", 5): 2500,
            }
        }

    orchestrator = AssessmentScoringOrchestrator(
        storage=storage,
        config=AssessmentScoringConfig(
            schema_id=FORGE_LENDING_SCHEMA_ID,
            horizons=(5,),
            universe_members=lambda tickers: tuple(int(ticker) for ticker in tickers),
            accepted_values=accepted_values,
            coordinate_for=lambda netuid, horizon, output: (
                AssessmentCoordinate.subnet_asset(
                    netuid=netuid, horizon_seconds=horizon, output=output
                )
            ),
            outputs=(
                ScoredOutputConfig(
                    output="alpha",
                    resolver=resolve_alpha,
                    spec=_Spec(
                        0,
                        100,
                        AggressiveDirection.HIGHER,
                        Decimal(1),
                        DeviationMode.ABSOLUTE,
                    ),
                ),
                ScoredOutputConfig(
                    output="beta",
                    resolver=resolve_beta,
                    spec=_Spec(
                        0,
                        1000,
                        AggressiveDirection.HIGHER,
                        Decimal(1),
                        DeviationMode.ABSOLUTE,
                    ),
                ),
            ),
        ),
        half_life_rounds=2,
    )

    round_scores = orchestrator.resolve_and_score(ROUND, 5, now_iso=NOW)

    targets = storage.assessment_realized_targets_for(ROUND, FORGE_LENDING_SCHEMA_ID)
    assert {target.coordinate.output for target in targets} == {"alpha", "beta"}
    scores = {
        score.coordinate.output: score.score
        for score in storage.assessment_output_scores_for_round(
            ROUND, FORGE_LENDING_SCHEMA_ID
        )
    }
    assert scores["alpha"] == Decimal(1)
    assert scores["beta"] == Decimal("0.5")
    assert round_scores == {"hk-a": Decimal("0.75")}


def test_scoring_pass_idempotency_is_per_horizon(storage: Storage) -> None:
    _open_round(storage)

    def resolve(
        _context: AssessmentResolutionContext, netuid: int, horizon: int
    ) -> AssessmentRealizedTarget:
        return AssessmentRealizedTarget(
            coordinate=AssessmentCoordinate.subnet_asset(
                netuid=netuid, horizon_seconds=horizon, output="alpha"
            ),
            value=Decimal(horizon),
            status="resolved",
        )

    orchestrator = AssessmentScoringOrchestrator(
        storage=storage,
        config=AssessmentScoringConfig(
            schema_id=FORGE_LENDING_SCHEMA_ID,
            horizons=(5, 30),
            universe_members=lambda tickers: tuple(int(ticker) for ticker in tickers),
            accepted_values=lambda round_id: {},
            coordinate_for=lambda netuid, horizon, output: (
                AssessmentCoordinate.subnet_asset(
                    netuid=netuid, horizon_seconds=horizon, output=output
                )
            ),
            outputs=(
                ScoredOutputConfig(
                    output="alpha",
                    resolver=resolve,
                    spec=_Spec(
                        0,
                        100,
                        AggressiveDirection.HIGHER,
                        Decimal(1),
                        DeviationMode.ABSOLUTE,
                    ),
                ),
            ),
        ),
        half_life_rounds=2,
    )

    orchestrator.resolve_and_score(ROUND, 5, now_iso=NOW)
    orchestrator.resolve_and_score(ROUND, 5, now_iso=NOW)
    orchestrator.resolve_and_score(ROUND, 30, now_iso=NOW)

    targets = storage.assessment_realized_targets_for(ROUND, FORGE_LENDING_SCHEMA_ID)
    assert [target.coordinate.horizon_value for target in targets] == [5, 30]


def test_active_ema_hotkey_is_zero_filled_when_it_skips_a_submission(
    storage: Storage,
) -> None:
    previous_round = "2026-07-02"
    storage.open_round(
        windows=compute_windows(date(2026, 7, 2), offsets=DEFAULT_OFFSETS),
        schema_id=FORGE_LENDING_SCHEMA_ID,
        universe=StaticLendingUniverseProvider(netuids=(44,)).fetch_universe(
            previous_round
        ),
        now_iso=NOW,
    )
    storage.record_commit(
        previous_round,
        FORGE_LENDING_SCHEMA_ID,
        "hk-active",
        "ab" * 32,
        now_iso=NOW,
    )
    storage.record_reveal(
        previous_round,
        FORGE_LENDING_SCHEMA_ID,
        "hk-active",
        bundle_json="{}",
        nonce_hex="cd" * 16,
        accepted=True,
        rejection_code=None,
        now_iso=NOW,
    )
    _open_round(storage)
    coordinate = _coordinate("alpha", 5)
    storage.upsert_assessment_ema(
        FORGE_LENDING_SCHEMA_ID,
        AssessmentEmaState(
            miner_hotkey="hk-active",
            coordinate=coordinate,
            ema=Decimal(1),
            resolved_rounds=1,
        ),
        now_iso=NOW,
    )

    def resolve(
        _context: AssessmentResolutionContext, netuid: int, horizon: int
    ) -> AssessmentRealizedTarget:
        return AssessmentRealizedTarget(
            coordinate=AssessmentCoordinate.subnet_asset(
                netuid=netuid, horizon_seconds=horizon, output="alpha"
            ),
            value=Decimal(100),
            status="resolved",
        )

    orchestrator = AssessmentScoringOrchestrator(
        storage=storage,
        config=AssessmentScoringConfig(
            schema_id=FORGE_LENDING_SCHEMA_ID,
            horizons=(5,),
            universe_members=lambda tickers: tuple(int(ticker) for ticker in tickers),
            accepted_values=lambda round_id: {},
            coordinate_for=lambda netuid, horizon, output: (
                AssessmentCoordinate.subnet_asset(
                    netuid=netuid, horizon_seconds=horizon, output=output
                )
            ),
            outputs=(
                ScoredOutputConfig(
                    output="alpha",
                    resolver=resolve,
                    spec=_Spec(
                        0,
                        100,
                        AggressiveDirection.HIGHER,
                        Decimal(1),
                        DeviationMode.ABSOLUTE,
                    ),
                ),
            ),
        ),
        half_life_rounds=2,
    )

    assert orchestrator.resolve_and_score(ROUND, 5, now_iso=NOW) == {
        "hk-active": Decimal(0)
    }
    [history] = storage.assessment_score_history_for_round(
        ROUND, FORGE_LENDING_SCHEMA_ID
    )
    assert history.miner_hotkey == "hk-active"
    assert history.round_score == Decimal(0)
