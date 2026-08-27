from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from endure.assessment.schemas.subnet_alpha_risk import (
    RISK_HORIZONS,
    RISK_SCHEMA_ID,
    RISK_SPECS_BY_OUTPUT,
    RiskAssetSubmission,
    RiskOutput,
    RiskOutputValue,
    RiskSubmissionBundle,
)
from endure.assessment.universe import UniverseSnapshot
from endure.protocol.round_engine import RoundWindows
from endure.protocol.schedulers import SyntheticScheduler
from endure.protocol.vertical import (
    AssessmentRoundProgram,
    RoundProgram,
    VerticalRuntime,
)
from endure.scoring.assessment_orchestrator import AssessmentScoringOrchestrator


class _AssessmentScorer(AssessmentScoringOrchestrator):
    def __init__(self) -> None:
        pass

    def resolve_and_score(
        self,
        round_id: str,
        horizon: int,
        *,
        now_iso: str,
        resolution_due_at: datetime | None = None,
        archive_hotkeys: Sequence[str] = (),
    ) -> dict[str, Decimal]:
        del round_id, horizon, now_iso, resolution_due_at, archive_hotkeys
        return {}

    def weights(self) -> dict[str, Decimal]:
        return {"miner": Decimal("0.9")}

    def blended_scores(self) -> dict[str, Decimal]:
        return {"miner": Decimal("0.3")}


class _EmptyUniverseProvider:
    def fetch_universe(self, round_id: str) -> UniverseSnapshot:
        return UniverseSnapshot(round_id=round_id, tickers=(), source_hash="empty")


def test_round_program_members_match_dispatch_inventory() -> None:
    # Given: the protocol introduced for the closed D1-D6 inventory.
    protocol_members = {
        name
        for name, value in vars(RoundProgram).items()
        if callable(value) and not name.startswith("_")
    }

    # When: its public callable surface is inspected.
    expected_members = {
        "weights",
        "blended_scores",
        "publish_consensus",
        "resolve_due",
    }

    # Then: no speculative operation exists beyond the four inventoried members.
    assert protocol_members == expected_members


def test_assessment_program_keeps_raw_blends_distinct_from_weights(storage) -> None:
    # Given: an assessment scorer whose raw blend differs from its final weight.
    program = AssessmentRoundProgram(
        storage=storage,
        schema_id=RISK_SCHEMA_ID,
        bundle_model=RiskSubmissionBundle,
        orchestrator=_AssessmentScorer(),
        horizons=(1,),
        due_seconds_by_horizon={},
    )

    # When: the D1 and D2 projections are read independently.
    weights = program.weights()
    blended_scores = program.blended_scores()

    # Then: emission provenance can retain the raw D2 snapshot.
    assert weights == {"miner": Decimal("0.9")}
    assert blended_scores == {"miner": Decimal("0.3")}


def test_vertical_runtime_carries_only_inventory_projections(storage) -> None:
    # Given: one assessment round program and its schema-specific scheduler.
    scheduler = SyntheticScheduler(
        sessions=(date(2026, 8, 12),),
        epoch=datetime(2026, 8, 12, tzinfo=UTC),
        period_seconds=60,
    )
    program = AssessmentRoundProgram(
        storage=storage,
        schema_id=RISK_SCHEMA_ID,
        bundle_model=RiskSubmissionBundle,
        orchestrator=_AssessmentScorer(),
        horizons=(1,),
        due_seconds_by_horizon={},
    )

    # When: the vertical runtime is composed.
    runtime = VerticalRuntime(
        round_program=program,
        publisher="risk",
        scheduler=scheduler,
    )

    # Then: D1-D6, D8-D9, and D11 each have one explicit projection.
    assert runtime.round_program is program
    assert runtime.publisher == "risk"
    assert runtime.scheduler is scheduler


def _risk_bundle_json(round_id: str) -> str:
    outputs = tuple(
        RiskOutputValue(
            output=output,
            value=value,
            confidence_bps=8000,
            reason_codes=("baseline",),
            horizon_seconds=horizon,
            unit=RISK_SPECS_BY_OUTPUT[output].unit,
        )
        for horizon in RISK_HORIZONS
        for output, value in (
            (RiskOutput.MAX_DRAWDOWN, 1500),
            (RiskOutput.REALIZED_VOLATILITY, 8000),
            (RiskOutput.TWAP_PRICE, 123_000_000),
            (RiskOutput.LIQUIDITY_DEPTH, 456_000_000),
        )
    )
    bundle = RiskSubmissionBundle(
        round_id=round_id,
        schema_id=RISK_SCHEMA_ID,
        assets=(RiskAssetSubmission(netuid=8, outputs=outputs),),
    )
    return bundle.model_dump_json()


def _record_accepted(storage, round_id: str, hotkey: str, bundle_json: str) -> None:
    now_iso = datetime(2026, 8, 12, 20, 30, tzinfo=UTC).isoformat()
    storage.record_commit(round_id, RISK_SCHEMA_ID, hotkey, "ab" * 32, now_iso=now_iso)
    storage.record_reveal(
        round_id,
        RISK_SCHEMA_ID,
        hotkey,
        bundle_json=bundle_json,
        nonce_hex="cd" * 16,
        accepted=True,
        rejection_code=None,
        now_iso=now_iso,
    )


def test_publish_consensus_skips_an_accepted_bundle_that_no_longer_parses(
    storage,
) -> None:
    # Given: an open round holding one parseable accepted bundle and one that
    # was accepted under an earlier contract but no longer satisfies the model.
    round_id = "2026-08-12"
    anchor = datetime(2026, 8, 12, 20, 0, tzinfo=UTC)
    windows = RoundWindows(
        round_id=round_id,
        commit_open=anchor,
        commit_close=anchor + timedelta(hours=1),
        t0_close=anchor + timedelta(hours=1),
        reveal_open=anchor + timedelta(hours=1),
        reveal_close=anchor + timedelta(hours=2),
    )
    storage.open_round(
        windows=windows,
        schema_id=RISK_SCHEMA_ID,
        universe=UniverseSnapshot(round_id=round_id, tickers=(), source_hash="u"),
        now_iso=anchor.isoformat(),
    )
    _record_accepted(storage, round_id, "miner", _risk_bundle_json(round_id))
    _record_accepted(
        storage, round_id, "stale", '{"schema_id": "risk.v1.subnet_alpha"}'
    )
    program = AssessmentRoundProgram(
        storage=storage,
        schema_id=RISK_SCHEMA_ID,
        bundle_model=RiskSubmissionBundle,
        orchestrator=_AssessmentScorer(),
        horizons=(1,),
        due_seconds_by_horizon={},
    )

    # When: consensus is published for the round.
    published = program.publish_consensus(round_id, anchor + timedelta(hours=2))

    # Then: the round reveals on the parseable bundle instead of wedging open.
    assert published is True
    rows = storage.assessment_consensus_for(round_id, RISK_SCHEMA_ID)
    assert rows
    assert {row.n_submitters for row in rows} == {1}
