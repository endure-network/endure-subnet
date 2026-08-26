"""Alpha Risk calendar-independent round scheduling (24/7 rounds spec)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from endure.assessment.registry import UniverseSnapshot
from endure.assessment.schemas.subnet_alpha_risk import (
    RISK_HORIZONS,
    RISK_SCHEMA_ID,
    RiskSubmissionBundle,
)
from endure.protocol.round_engine import DEFAULT_OFFSETS, compute_windows
from endure.protocol.schedulers import FixedUtcScheduler, scheduler_for_schema
from endure.protocol.validator_service import ValidatorRoundService
from endure.protocol.vertical import AssessmentRoundProgram
from endure.storage.repository import Storage


class _EveryDayRiskUniverse:
    def fetch_universe(self, round_id: str) -> UniverseSnapshot:
        return UniverseSnapshot(
            round_id=round_id,
            tickers=("44",),
            source_hash="test-fixed-utc-risk-universe",
        )


class _RiskAssessmentScorer:
    def __init__(self, storage: Storage) -> None:
        self._storage = storage
        self.resolution_calls: list[tuple[str, int, datetime]] = []

    @property
    def horizons(self) -> tuple[int, ...]:
        return RISK_HORIZONS

    def resolve_and_score(
        self,
        round_id: str,
        horizon: int,
        *,
        now_iso: str,
        resolution_due_at: datetime | None = None,
        archive_hotkeys: Sequence[str] = (),
    ) -> dict[str, Decimal]:
        assert resolution_due_at is not None
        self.resolution_calls.append((round_id, horizon, resolution_due_at))
        self._storage.record_assessment_scoring_pass(
            round_id,
            RISK_SCHEMA_ID,
            horizon_value=horizon,
            realized_targets=(),
            output_scores=(),
            ema_updates=(),
            score_history=(),
            now_iso=now_iso,
        )
        return {}

    def weights(self) -> dict[str, Decimal]:
        return {}

    def blended_scores(self) -> dict[str, Decimal]:
        return {}


class TestFixedUtcScheduler:
    def test_uses_the_fixed_utc_window_shape_on_an_nyse_holiday(self) -> None:
        scheduler = FixedUtcScheduler(fetch_delay_seconds=0)

        window = scheduler.active_window(datetime(2026, 6, 19, 15, tzinfo=UTC))

        assert window is not None
        assert window.round_id == "2026-06-19"
        assert window.commit_open == datetime(2026, 6, 19, 11, tzinfo=UTC)
        assert window.commit_close == datetime(2026, 6, 19, 19, 30, tzinfo=UTC)
        assert window.t0_close == datetime(2026, 6, 19, 20, tzinfo=UTC)
        assert window.reveal_open == datetime(2026, 6, 19, 20, 30, tzinfo=UTC)
        assert window.reveal_close == datetime(2026, 6, 20, 0, tzinfo=UTC)

    def test_creates_a_round_on_a_weekend(self) -> None:
        scheduler = FixedUtcScheduler(fetch_delay_seconds=0)

        window = scheduler.active_window(datetime(2026, 6, 20, 15, tzinfo=UTC))

        assert window is not None
        assert window.round_id == "2026-06-20"

    def test_keeps_january_and_july_windows_on_the_same_utc_clock(self) -> None:
        scheduler = FixedUtcScheduler(fetch_delay_seconds=0)

        january = scheduler.active_window(datetime(2026, 1, 5, 15, tzinfo=UTC))
        july = scheduler.active_window(datetime(2026, 7, 6, 15, tzinfo=UTC))

        assert january is not None
        assert july is not None
        assert january.commit_open.timetz() == july.commit_open.timetz()
        assert january.commit_close.timetz() == july.commit_close.timetz()
        assert january.t0_close.timetz() == july.t0_close.timetz()
        assert january.reveal_open.timetz() == july.reveal_open.timetz()
        assert january.reveal_close.timetz() == july.reveal_close.timetz()

    def test_uses_calendar_days_for_resolution_due(self) -> None:
        scheduler = FixedUtcScheduler(fetch_delay_seconds=3600)

        before_due = datetime(2026, 6, 24, 20, 59, tzinfo=UTC)
        due = datetime(2026, 6, 24, 21, tzinfo=UTC)

        assert scheduler.resolution_due("2026-06-19", 5, before_due) is False
        assert scheduler.resolution_due("2026-06-19", 5, due) is True


class TestFixedUtcCutover:
    def test_closes_stored_nyse_risk_round_while_opening_fixed_utc_weekend_round(
        self, storage: Storage
    ) -> None:
        universe_provider = _EveryDayRiskUniverse()
        old_round_id = "2023-03-06"
        old_windows = compute_windows(
            date.fromisoformat(old_round_id), offsets=DEFAULT_OFFSETS
        )
        assert old_windows.t0_close == datetime(2023, 3, 6, 21, tzinfo=UTC)
        storage.open_round(
            windows=old_windows,
            schema_id=RISK_SCHEMA_ID,
            universe=universe_provider.fetch_universe(old_round_id),
            now_iso=old_windows.commit_open.isoformat(),
        )
        now_holder = {"now": datetime(2023, 3, 11, 15, tzinfo=UTC)}
        scheduler = scheduler_for_schema(RISK_SCHEMA_ID, fetch_delay_seconds=0)
        assert isinstance(scheduler, FixedUtcScheduler)
        scorer = _RiskAssessmentScorer(storage)
        service = ValidatorRoundService(
            storage=storage,
            scheduler=scheduler,
            universe_provider=universe_provider,
            schema_id=RISK_SCHEMA_ID,
            horizons=RISK_HORIZONS,
            now_fn=lambda: now_holder["now"],
            round_program=AssessmentRoundProgram(
                storage=storage,
                schema_id=RISK_SCHEMA_ID,
                bundle_model=RiskSubmissionBundle,
                orchestrator=scorer,
                horizons=RISK_HORIZONS,
                due_seconds_by_horizon={},
            ),
        )

        service.tick(expected_miners=())

        weekend_round_id = "2023-03-11"
        weekend_windows = storage.round_windows(weekend_round_id, RISK_SCHEMA_ID)
        assert weekend_windows is not None
        assert weekend_windows.t0_close == datetime(2023, 3, 11, 20, tzinfo=UTC)
        assert storage.round_state(old_round_id, RISK_SCHEMA_ID) == "revealed"
        assert storage.round_state(weekend_round_id, RISK_SCHEMA_ID) == "open"

        now_holder["now"] = old_windows.reveal_close + timedelta(
            seconds=max(RISK_HORIZONS) + 1
        )
        assert service.tick(expected_miners=()) == {}

        assert storage.round_windows(old_round_id, RISK_SCHEMA_ID) == old_windows
        assert storage.round_state(old_round_id, RISK_SCHEMA_ID) == "closed"
        assert (
            storage.round_state(weekend_round_id, RISK_SCHEMA_ID) == "partially_scored"
        )
        assert [
            call for call in scorer.resolution_calls if call[0] == old_round_id
        ] == [
            (
                old_round_id,
                horizon,
                old_windows.reveal_close + timedelta(seconds=horizon),
            )
            for horizon in RISK_HORIZONS
        ]
