"""Validator + miner round services over the synthetic scheduler (spec §2, §11)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol

import pytest

from endure.assessment.coordinates import (
    AssessmentCoordinate,
    AssessmentEmaState,
    AssessmentRealizedTarget,
)
from endure.assessment.lending_universe import StaticLendingUniverseProvider
from endure.assessment.registry import UniverseProvider, UniverseSnapshot
from endure.assessment.schemas.forge_lending import (
    FORGE_LENDING_SCHEMA_ID,
    LENDING_HORIZON_SECONDS,
    AggressiveDirection,
    DeviationMode,
    LendingSubmissionBundle,
)
from endure.protocol.bundles import AssembledSubmission
from endure.protocol.canonical import (
    COMMIT_NONCE_BYTES,
    canonical_bundle_bytes,
    commit_hash,
)
from endure.protocol.lending_miner import (
    LendingBaselineAssembler,
    baseline_lending_bundle,
)
from endure.protocol.miner_service import MinerRoundService
from endure.protocol.schedulers import SyntheticScheduler
from endure.protocol.synapses import SubmitCommit, SubmitReveal
from endure.protocol.validator_service import ValidatorRoundService
from endure.protocol.vertical import (
    AssessmentRoundProgram,
    RoundProgram,
)
from endure.scoring.assessment_orchestrator import (
    NEUTRAL_RESOLUTION_CONTEXT,
    AssessmentResolutionContext,
    AssessmentScoringConfig,
    AssessmentScoringOrchestrator,
    ScoredOutputConfig,
)
from endure.scoring.lending.market_data import recorded_mainnet_fixture_provider
from endure.scoring.lending.orchestrator import LendingScoringOrchestrator
from endure.storage.repository import Storage

SESSIONS = (
    date(2023, 3, 6),
    date(2023, 3, 7),
    date(2023, 3, 8),
    date(2023, 3, 9),
    date(2023, 3, 10),
)
UNIVERSE = ("GOOD",)
EPOCH = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)
PERIOD = 100


def _record_accepted_submission(
    storage: Storage, round_id: str, hotkey: str, *, now_iso: str
) -> None:
    storage.record_commit(
        round_id,
        FORGE_LENDING_SCHEMA_ID,
        hotkey,
        "ab" * 32,
        now_iso=now_iso,
    )
    storage.record_reveal(
        round_id,
        FORGE_LENDING_SCHEMA_ID,
        hotkey,
        bundle_json="{}",
        nonce_hex="cd" * 16,
        accepted=True,
        rejection_code=None,
        now_iso=now_iso,
    )


@dataclass(frozen=True, slots=True)
class _AssessmentSpec:
    grace_band: int
    cutoff: int
    aggressive_direction: AggressiveDirection
    lenient_multiplier: Decimal
    deviation_mode: DeviationMode
    scored_live: bool = True


class _StaticUniverseProvider:
    def fetch_universe(self, round_id: str) -> UniverseSnapshot:
        return UniverseSnapshot(
            round_id=round_id,
            tickers=UNIVERSE,
            source_hash="static-test-universe",
        )


class _RecordingAssessmentOrchestrator:
    def __init__(self, storage: Storage) -> None:
        self._storage = storage
        self.scored: list[tuple[str, int]] = []

    def resolve_and_score(
        self,
        round_id: str,
        horizon: int,
        *,
        now_iso: str,
        resolution_due_at: datetime | None = None,
        archive_hotkeys: Sequence[str] = (),
    ) -> dict[str, Decimal]:
        del resolution_due_at, archive_hotkeys
        self.scored.append((round_id, horizon))
        self._storage.record_assessment_scoring_pass(
            round_id,
            FORGE_LENDING_SCHEMA_ID,
            horizon_value=horizon,
            realized_targets=(),
            output_scores=(),
            ema_updates=(),
            score_history=(),
            now_iso=now_iso,
        )
        return {"hk-a": Decimal(1)}

    def weights(self) -> dict[str, Decimal]:
        return {"hk-a": Decimal(1)}

    def blended_scores(self) -> dict[str, Decimal]:
        return {"hk-a": Decimal(1)}


class _AssessmentOrchestrator(Protocol):
    def resolve_and_score(
        self,
        round_id: str,
        horizon: int,
        *,
        now_iso: str,
        resolution_due_at: datetime | None = None,
        archive_hotkeys: Sequence[str] = (),
    ) -> dict[str, Decimal]: ...

    def weights(self) -> dict[str, Decimal]: ...

    def blended_scores(self) -> dict[str, Decimal]: ...


class _AssessmentOrchestratorAdapter(AssessmentScoringOrchestrator):
    def __init__(self, delegate: _AssessmentOrchestrator) -> None:
        self._delegate = delegate

    def resolve_and_score(
        self,
        round_id: str,
        horizon: int | None = None,
        *,
        now_iso: str,
        resolution_due_at: datetime | None = None,
        archive_hotkeys: Sequence[str] = (),
        context: AssessmentResolutionContext = NEUTRAL_RESOLUTION_CONTEXT,
    ) -> dict[str, Decimal]:
        del context
        if horizon is None:
            raise ValueError(horizon)
        return self._delegate.resolve_and_score(
            round_id,
            horizon,
            now_iso=now_iso,
            resolution_due_at=resolution_due_at,
            archive_hotkeys=archive_hotkeys,
        )

    def weights(self) -> dict[str, Decimal]:
        return self._delegate.weights()

    def blended_scores(self) -> dict[str, Decimal]:
        return self._delegate.blended_scores()


class _MissingAssessmentRoundProgram:
    def weights(self) -> dict[str, Decimal]:
        raise RuntimeError("assessment schema requires an assessment orchestrator")

    def blended_scores(self) -> dict[str, Decimal]:
        raise RuntimeError("assessment schema requires an assessment orchestrator")

    def publish_consensus(self, round_id: str, now: datetime) -> bool:
        del round_id, now
        raise RuntimeError("assessment schema requires an assessment orchestrator")

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
        raise RuntimeError("assessment schema requires an assessment orchestrator")


def _validator_service(
    storage: Storage,
    now_holder: dict[str, datetime],
    *,
    universe_provider: UniverseProvider | None = None,
    lending_orchestrator: _AssessmentOrchestrator | None = None,
    horizons: tuple[int, ...] = (1,),
    sessions: tuple[date, ...] = SESSIONS,
    schema_id: str = FORGE_LENDING_SCHEMA_ID,
    max_universe_targets: int | None = None,
) -> ValidatorRoundService:
    scheduler = SyntheticScheduler(
        sessions=sessions, epoch=EPOCH, period_seconds=PERIOD
    )
    universe = universe_provider or _StaticUniverseProvider()
    orchestrator = lending_orchestrator or _RecordingAssessmentOrchestrator(storage)
    due_seconds_by_horizon = (
        {horizon: horizon * PERIOD for horizon in horizons}
        if horizons in ((1,), (5, 1))
        else {}
    )
    round_program: RoundProgram = AssessmentRoundProgram(
        storage=storage,
        schema_id=schema_id,
        bundle_model=LendingSubmissionBundle,
        orchestrator=_AssessmentOrchestratorAdapter(orchestrator),
        horizons=horizons,
        due_seconds_by_horizon=due_seconds_by_horizon,
    )
    return ValidatorRoundService(
        storage=storage,
        scheduler=scheduler,
        universe_provider=universe,
        schema_id=schema_id,
        horizons=horizons,
        now_fn=lambda: now_holder["now"],
        max_universe_targets=max_universe_targets,
        round_program=round_program,
    )


class TestValidatorRoundService:
    def test_opens_round_during_active_window(self, storage: Storage) -> None:
        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        service = _validator_service(storage, now_holder)

        service.tick(expected_miners=("hk-a",))

        assert storage.round_state("2023-03-06", FORGE_LENDING_SCHEMA_ID) == "open"
        universe = storage.universe_for("2023-03-06", FORGE_LENDING_SCHEMA_ID)
        assert universe is not None
        assert universe.tickers == UNIVERSE

    def test_round_reveals_after_embargo_and_scores_when_due(
        self, storage: Storage
    ) -> None:
        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        service = _validator_service(storage, now_holder)
        service.tick(expected_miners=("hk-a",))

        # Past reveal close of round 0, inside round 1.
        now_holder["now"] = EPOCH + timedelta(seconds=110)
        service.tick(expected_miners=("hk-a",))
        assert storage.round_state("2023-03-06", FORGE_LENDING_SCHEMA_ID) == "revealed"

        # Past resolution for (round 0, horizon 1) → scored and closed.
        now_holder["now"] = EPOCH + timedelta(seconds=210)
        weights = service.tick(expected_miners=("hk-a",))

        assert storage.has_assessment_resolution_marker(
            "2023-03-06", FORGE_LENDING_SCHEMA_ID, 1
        )
        assert storage.round_state("2023-03-06", FORGE_LENDING_SCHEMA_ID) == "closed"
        assert weights is not None and "hk-a" in weights

    def test_scoring_excludes_reveal_persisted_after_consensus(
        self, storage: Storage
    ) -> None:
        class _ConsensusScoringAssessment(_RecordingAssessmentOrchestrator):
            def resolve_and_score(
                self,
                round_id: str,
                horizon: int,
                *,
                now_iso: str,
                resolution_due_at: datetime | None = None,
                archive_hotkeys: Sequence[str] = (),
            ) -> dict[str, Decimal]:
                super().resolve_and_score(
                    round_id,
                    horizon,
                    now_iso=now_iso,
                    resolution_due_at=resolution_due_at,
                    archive_hotkeys=archive_hotkeys,
                )
                scoring_hotkeys = {
                    hotkey
                    for hotkey, _ in storage.scoring_bundles(
                        round_id, FORGE_LENDING_SCHEMA_ID
                    )
                }
                return {
                    hotkey: Decimal(hotkey in scoring_hotkeys)
                    for hotkey in ("hk-early", "hk-late")
                }

        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        orchestrator = _ConsensusScoringAssessment(storage)
        service = _validator_service(
            storage, now_holder, lending_orchestrator=orchestrator
        )
        round_id = "2023-03-06"
        early_bundle = baseline_lending_bundle(round_id=round_id, netuids=(44,))
        early_json = canonical_bundle_bytes(early_bundle.to_canonical_payload()).decode(
            "utf-8"
        )
        early_nonce = b"\x01" * COMMIT_NONCE_BYTES
        early_hash = commit_hash(
            early_json.encode(), early_nonce, miner_hotkey="hk-early"
        )

        service.tick(expected_miners=("hk-early", "hk-late"))
        storage.record_commit(
            round_id,
            FORGE_LENDING_SCHEMA_ID,
            "hk-early",
            early_hash,
            now_iso=now_holder["now"].isoformat(),
        )
        storage.record_reveal(
            round_id,
            FORGE_LENDING_SCHEMA_ID,
            "hk-early",
            bundle_json=early_json,
            nonce_hex=early_nonce.hex(),
            accepted=True,
            rejection_code=None,
            now_iso=now_holder["now"].isoformat(),
        )

        now_holder["now"] = EPOCH + timedelta(seconds=PERIOD + 10)
        service.tick(expected_miners=("hk-early", "hk-late"))
        late_nonce = b"\x02" * COMMIT_NONCE_BYTES
        late_hash = commit_hash(early_json.encode(), late_nonce, miner_hotkey="hk-late")
        storage.record_commit(
            round_id,
            FORGE_LENDING_SCHEMA_ID,
            "hk-late",
            late_hash,
            now_iso=now_holder["now"].isoformat(),
        )
        storage.record_reveal(
            round_id,
            FORGE_LENDING_SCHEMA_ID,
            "hk-late",
            bundle_json=early_json,
            nonce_hex=late_nonce.hex(),
            accepted=True,
            rejection_code=None,
            now_iso=now_holder["now"].isoformat(),
        )

        consensus = storage.assessment_consensus_for(round_id, FORGE_LENDING_SCHEMA_ID)
        assert consensus
        assert all(row.n_submitters == 1 for row in consensus)
        assert [
            hotkey
            for hotkey, _ in storage.scoring_bundles(round_id, FORGE_LENDING_SCHEMA_ID)
        ] == ["hk-early"]

        scores = orchestrator.resolve_and_score(
            round_id,
            1,
            now_iso=now_holder["now"].isoformat(),
        )

        assert orchestrator.scored == [(round_id, 1)]
        assert scores["hk-early"] > Decimal(0)
        assert scores["hk-late"] == Decimal(0)

    def test_rounds_revealing_without_submissions_accumulate_empty_counter(
        self, storage: Storage
    ) -> None:
        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        service = _validator_service(storage, now_holder)
        service.tick(expected_miners=("hk-a",))
        assert service.consecutive_empty_scored_rounds == 0

        now_holder["now"] = EPOCH + timedelta(seconds=110)
        service.tick(expected_miners=("hk-a",))
        assert storage.round_state("2023-03-06", FORGE_LENDING_SCHEMA_ID) == "revealed"
        assert service.consecutive_empty_scored_rounds == 1

        now_holder["now"] = EPOCH + timedelta(seconds=210)
        service.tick(expected_miners=("hk-a",))
        assert service.consecutive_empty_scored_rounds == 2

    def test_scoring_one_round_does_not_relabel_other_revealed_rounds(
        self, storage: Storage
    ) -> None:
        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        service = _validator_service(storage, now_holder)
        service.tick(expected_miners=("hk-a",))

        now_holder["now"] = EPOCH + timedelta(seconds=110)
        service.tick(expected_miners=("hk-a",))

        # Round 0 scores and closes; round 1 reveals in the same tick but has
        # no due horizon — it must stay "revealed", not "partially_scored".
        now_holder["now"] = EPOCH + timedelta(seconds=210)
        service.tick(expected_miners=("hk-a",))

        assert storage.round_state("2023-03-06", FORGE_LENDING_SCHEMA_ID) == "closed"
        assert storage.round_state("2023-03-07", FORGE_LENDING_SCHEMA_ID) == "revealed"

    def test_one_unresolvable_round_does_not_wedge_newer_rounds(
        self, storage: Storage
    ) -> None:
        """A round that always fails to resolve must not block every newer
        round forever: containment is per-round, the failure surfaces on
        /health, and a healthy newer round still resolves."""
        real = _RecordingAssessmentOrchestrator(storage)

        class _WedgedOnFirstRound:
            """Delegates everything, but the oldest round can never resolve."""

            def __init__(self) -> None:
                self.scored: list[str] = []

            def resolve_and_score(
                self,
                round_id: str,
                horizon: int,
                *,
                now_iso: str,
                resolution_due_at: datetime | None = None,
                archive_hotkeys: Sequence[str] = (),
            ) -> dict[str, Decimal]:
                if round_id == "2023-03-06":
                    raise RuntimeError("provider gap that never heals")
                self.scored.append(round_id)
                return real.resolve_and_score(
                    round_id,
                    horizon,
                    now_iso=now_iso,
                    resolution_due_at=resolution_due_at,
                    archive_hotkeys=archive_hotkeys,
                )

            def weights(self) -> dict[str, Decimal]:
                return real.weights()

            def blended_scores(self) -> dict[str, Decimal]:
                return real.blended_scores()

        orchestrator = _WedgedOnFirstRound()
        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        service = _validator_service(
            storage, now_holder, lending_orchestrator=orchestrator
        )

        service.tick(expected_miners=("hk-a",))  # open round 0 (2023-03-06)
        now_holder["now"] = EPOCH + timedelta(seconds=110)
        service.tick(expected_miners=("hk-a",))  # open round 1, reveal round 0
        now_holder["now"] = EPOCH + timedelta(seconds=210)
        service.tick(expected_miners=("hk-a",))  # reveal round 1
        # Jump far past every horizon's resolution: both rounds are due.
        now_holder["now"] = EPOCH + timedelta(seconds=100_000)
        service.tick(expected_miners=("hk-a",))

        # The wedged oldest round never closes, but the newer round resolved
        # despite being iterated after it — and the failure is observable.
        assert storage.round_state("2023-03-06", FORGE_LENDING_SCHEMA_ID) != "closed"
        assert storage.has_assessment_resolution_marker(
            "2023-03-07", FORGE_LENDING_SCHEMA_ID, 1
        )
        assert "2023-03-07" in orchestrator.scored
        assert service.consecutive_resolution_failures >= 1
        assert service.last_resolution_error == "RuntimeError"

    def test_one_failing_horizon_does_not_block_a_due_sibling_horizon(
        self, storage: Storage
    ) -> None:
        """Per-horizon containment: a horizon whose resolution fails must not
        stop a later due horizon in the same round from resolving — and the
        failure still surfaces on /health (the per-round catch would otherwise
        skip every horizon after the first failure)."""
        real = _RecordingAssessmentOrchestrator(storage)

        class _HorizonFlaky:
            """Horizon 5 can never resolve; horizon 1 resolves normally."""

            def __init__(self) -> None:
                self.scored: list[tuple[str, int]] = []

            def resolve_and_score(
                self,
                round_id: str,
                horizon: int,
                *,
                now_iso: str,
                resolution_due_at: datetime | None = None,
                archive_hotkeys: Sequence[str] = (),
            ) -> dict[str, Decimal]:
                if horizon == 5:
                    raise RuntimeError("horizon 5 provider gap")
                self.scored.append((round_id, horizon))
                return real.resolve_and_score(
                    round_id,
                    horizon,
                    now_iso=now_iso,
                    resolution_due_at=resolution_due_at,
                    archive_hotkeys=archive_hotkeys,
                )

            def weights(self) -> dict[str, Decimal]:
                return real.weights()

            def blended_scores(self) -> dict[str, Decimal]:
                return real.blended_scores()

        orchestrator = _HorizonFlaky()
        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        # Extra scheduler sessions so horizon 5 is also "due"; only round 0 is
        # ever opened, so the fixture's bars (the first five sessions) suffice.
        sessions = SESSIONS + (date(2023, 3, 13), date(2023, 3, 14), date(2023, 3, 15))
        service = _validator_service(
            storage,
            now_holder,
            lending_orchestrator=orchestrator,
            horizons=(5, 1),
            sessions=sessions,
        )

        service.tick(expected_miners=("hk-a",))  # open round 0
        # Jump far past both horizons' resolution: reveal + resolve in one tick.
        now_holder["now"] = EPOCH + timedelta(seconds=100_000)
        service.tick(expected_miners=("hk-a",))

        # Horizon 1 resolved despite horizon 5 (iterated first) failing; the
        # round stays open (horizon 5 unresolved) and the failure is observable.
        assert storage.has_assessment_resolution_marker(
            "2023-03-06", FORGE_LENDING_SCHEMA_ID, 1
        )
        assert not storage.has_assessment_resolution_marker(
            "2023-03-06", FORGE_LENDING_SCHEMA_ID, 5
        )
        assert storage.round_state("2023-03-06", FORGE_LENDING_SCHEMA_ID) != "closed"
        assert ("2023-03-06", 1) in orchestrator.scored
        assert service.consecutive_resolution_failures >= 1
        assert service.last_resolution_error == "RuntimeError"

    def test_universe_fetch_failure_is_surfaced_not_swallowed(
        self, storage: Storage
    ) -> None:
        """A swallowed universe-fetch failure must not read as healthy: no
        round opens, but the service counts the failure so /health can
        degrade — a silent 'no round opened' is the worst soak failure."""

        class _FailingUniverse:
            def fetch_universe(self, round_id: str) -> UniverseSnapshot:
                raise RuntimeError("SSGA unreachable")

        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        service = _validator_service(
            storage, now_holder, universe_provider=_FailingUniverse()
        )

        service.tick(expected_miners=("hk-a",))

        assert storage.round_state("2023-03-06", FORGE_LENDING_SCHEMA_ID) is None
        assert service.consecutive_universe_failures == 1
        assert service.last_universe_error == "RuntimeError"

    def test_universe_failure_clears_after_a_successful_open(
        self, storage: Storage
    ) -> None:
        """One transient failure then recovery within the same window: the
        round opens and the counter resets, so the degrade self-heals."""
        good = _StaticUniverseProvider()

        class _FlakyUniverse:
            def __init__(self) -> None:
                self.calls = 0

            def fetch_universe(self, round_id: str) -> UniverseSnapshot:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("transient")
                return good.fetch_universe(round_id)

        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        service = _validator_service(
            storage, now_holder, universe_provider=_FlakyUniverse()
        )

        service.tick(expected_miners=("hk-a",))
        assert service.consecutive_universe_failures == 1

        service.tick(expected_miners=("hk-a",))
        assert storage.round_state("2023-03-06", FORGE_LENDING_SCHEMA_ID) == "open"
        assert service.consecutive_universe_failures == 0
        assert service.last_universe_error is None

    def test_rejects_universe_snapshot_for_wrong_round_id(
        self, storage: Storage
    ) -> None:
        class _WrongRoundUniverse:
            def fetch_universe(self, round_id: str) -> UniverseSnapshot:
                del round_id
                return UniverseSnapshot(
                    round_id="2023-03-07",
                    tickers=("GOOD",),
                    source_hash="wrong-round",
                )

        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        service = _validator_service(
            storage,
            now_holder,
            universe_provider=_WrongRoundUniverse(),
        )

        service.tick(expected_miners=("hk-a",))

        assert storage.round_state("2023-03-06", FORGE_LENDING_SCHEMA_ID) is None
        assert service.consecutive_universe_failures == 1
        assert service.last_universe_error == "ValueError"

    def test_lending_round_opens_with_static_netuid_universe(
        self, storage: Storage
    ) -> None:
        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        service = _validator_service(
            storage,
            now_holder,
            universe_provider=StaticLendingUniverseProvider(netuids=(44, 8)),
            schema_id=FORGE_LENDING_SCHEMA_ID,
            max_universe_targets=2,
        )

        service.tick(expected_miners=("hk-a",))

        universe = storage.universe_for("2023-03-06", FORGE_LENDING_SCHEMA_ID)
        assert universe is not None
        assert universe.tickers == ("8", "44")

    def test_lending_consensus_without_orchestrator_fails_legibly(
        self, storage: Storage
    ) -> None:
        # A lending service wired without its scoring orchestrator is a
        # misconfiguration: consensus must fail with a legible error naming
        # the missing seam, not parse bundles with the wrong model and surface an
        # opaque ValidationError that reads as data corruption.
        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        service = _validator_service(
            storage,
            now_holder,
            universe_provider=StaticLendingUniverseProvider(netuids=(44,)),
            lending_orchestrator=_MissingAssessmentRoundProgram(),
            schema_id=FORGE_LENDING_SCHEMA_ID,
        )

        with pytest.raises(RuntimeError, match="assessment orchestrator"):
            service._publish_consensus_and_reveal("2023-03-06", now_holder["now"])

    def test_lending_round_advances_open_reveal_scored_closed(
        self, storage: Storage
    ) -> None:
        # The Batch 7 scored-live walking skeleton: a lending round opens,
        # reaches generic consensus at reveal close, resolves the CF target
        # one lending horizon later, scores the accepted bundle, and closes.
        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        lending = LendingScoringOrchestrator(
            storage=storage,
            price_provider=recorded_mainnet_fixture_provider(),
            half_life_rounds=2,
        )
        service = _validator_service(
            storage,
            now_holder,
            universe_provider=StaticLendingUniverseProvider(netuids=(44, 8)),
            lending_orchestrator=lending,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            horizons=(LENDING_HORIZON_SECONDS,),
        )
        round_id = "2023-03-06"

        service.tick(expected_miners=("hk-a",))
        assert storage.round_state(round_id, FORGE_LENDING_SCHEMA_ID) == "open"

        bundle = baseline_lending_bundle(round_id=round_id, netuids=(44, 8))
        bundle_json = canonical_bundle_bytes(bundle.to_canonical_payload()).decode(
            "utf-8"
        )
        storage.record_commit(
            round_id,
            FORGE_LENDING_SCHEMA_ID,
            "hk-a",
            "ab" * 32,
            now_iso=now_holder["now"].isoformat(),
        )
        storage.record_reveal(
            round_id,
            FORGE_LENDING_SCHEMA_ID,
            "hk-a",
            bundle_json=bundle_json,
            nonce_hex="cd" * 16,
            accepted=True,
            rejection_code=None,
            now_iso=now_holder["now"].isoformat(),
        )

        # Past reveal close: generic consensus is published atomically with
        # the reveal flip, but the CF horizon has not elapsed yet.
        now_holder["now"] = EPOCH + timedelta(seconds=PERIOD)
        service.tick(expected_miners=("hk-a",))
        assert storage.round_state(round_id, FORGE_LENDING_SCHEMA_ID) == "revealed"
        consensus = storage.assessment_consensus_for(round_id, FORGE_LENDING_SCHEMA_ID)
        assert len(consensus) == 14  # 2 netuids x 7 outputs
        assert all(row.n_submitters == 1 for row in consensus)
        assert not storage.assessment_realized_targets_for(
            round_id, FORGE_LENDING_SCHEMA_ID
        )
        storage.record_commit(
            round_id,
            FORGE_LENDING_SCHEMA_ID,
            "hk-late",
            "ab" * 32,
            now_iso=now_holder["now"].isoformat(),
        )
        storage.record_reveal(
            round_id,
            FORGE_LENDING_SCHEMA_ID,
            "hk-late",
            bundle_json=bundle_json,
            nonce_hex="cd" * 16,
            accepted=True,
            rejection_code=None,
            now_iso=now_holder["now"].isoformat(),
        )

        # One lending horizon after reveal close: the CF target resolves, the
        # bundle is scored, the round closes, and lending weights come back.
        now_holder["now"] = EPOCH + timedelta(
            seconds=PERIOD + LENDING_HORIZON_SECONDS + 1
        )
        weights = service.tick(expected_miners=("hk-a",))
        assert storage.round_state(round_id, FORGE_LENDING_SCHEMA_ID) == "closed"
        targets = storage.assessment_realized_targets_for(
            round_id, FORGE_LENDING_SCHEMA_ID
        )
        assert {target.status for target in targets} == {"resolved"}
        assert (
            storage.assessment_output_score_count(round_id, FORGE_LENDING_SCHEMA_ID)
            == 2
        )
        assert weights is not None and set(weights) == {"hk-a"}
        assert weights["hk-a"] > Decimal(0)

    def test_assessment_resolution_runs_each_due_horizon_before_closing(
        self, storage: Storage
    ) -> None:
        class _TwoHorizonAssessment:
            def __init__(self) -> None:
                self.calls: list[int] = []

            def resolve_and_score(
                self,
                round_id: str,
                horizon: int,
                *,
                now_iso: str,
                resolution_due_at: datetime | None = None,
                archive_hotkeys: Sequence[str] = (),
            ) -> dict[str, Decimal]:
                self.calls.append(horizon)
                storage.record_assessment_scoring_pass(
                    round_id,
                    FORGE_LENDING_SCHEMA_ID,
                    horizon_value=horizon,
                    realized_targets=(
                        AssessmentRealizedTarget(
                            coordinate=AssessmentCoordinate.subnet_asset(
                                netuid=44, horizon_seconds=horizon, output="synthetic"
                            ),
                            value=Decimal(horizon),
                            status="resolved",
                        ),
                    ),
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

        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        assessment = _TwoHorizonAssessment()
        service = _validator_service(
            storage,
            now_holder,
            universe_provider=StaticLendingUniverseProvider(netuids=(44,)),
            lending_orchestrator=assessment,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            horizons=(5, 30),
        )
        round_id = "2023-03-06"

        service.tick(expected_miners=())
        now_holder["now"] = EPOCH + timedelta(seconds=PERIOD)
        service.tick(expected_miners=())

        assert assessment.calls == [5]
        assert (
            storage.round_state(round_id, FORGE_LENDING_SCHEMA_ID) == "partially_scored"
        )

        now_holder["now"] = EPOCH + timedelta(seconds=PERIOD + 31)
        service.tick(expected_miners=())
        service.tick(expected_miners=())

        assert assessment.calls == [5, 30]
        assert storage.round_state(round_id, FORGE_LENDING_SCHEMA_ID) == "closed"

    def test_archive_hotkeys_propagate_from_tick_to_assessment_scorer(
        self, storage: Storage
    ) -> None:
        class _RecordingAssessment:
            def __init__(self) -> None:
                self.archive_calls: list[tuple[str, ...]] = []

            def resolve_and_score(
                self,
                round_id: str,
                horizon: int,
                *,
                now_iso: str,
                resolution_due_at: datetime | None = None,
                archive_hotkeys: Sequence[str] = (),
            ) -> dict[str, Decimal]:
                self.archive_calls.append(tuple(archive_hotkeys))
                storage.record_assessment_scoring_pass(
                    round_id,
                    FORGE_LENDING_SCHEMA_ID,
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

        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        assessment = _RecordingAssessment()
        service = _validator_service(
            storage,
            now_holder,
            universe_provider=StaticLendingUniverseProvider(netuids=(44,)),
            lending_orchestrator=assessment,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            horizons=(5,),
        )
        service.tick(expected_miners=())
        now_holder["now"] = EPOCH + timedelta(seconds=PERIOD)
        service.tick(expected_miners=(), archive_hotkeys=("hk-gone",))

        assert assessment.archive_calls == [()]

    def test_archival_zero_fills_staggered_horizons_before_deleting_ema_state(
        self, storage: Storage
    ) -> None:
        def coordinate(horizon: int) -> AssessmentCoordinate:
            return AssessmentCoordinate.subnet_asset(
                netuid=44, horizon_seconds=horizon, output="alpha"
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

        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        assessment = AssessmentScoringOrchestrator(
            storage=storage,
            config=AssessmentScoringConfig(
                schema_id=FORGE_LENDING_SCHEMA_ID,
                horizons=(5, 30),
                universe_members=lambda tickers: tuple(
                    int(ticker) for ticker in tickers
                ),
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
                        spec=_AssessmentSpec(
                            grace_band=0,
                            cutoff=100,
                            aggressive_direction=AggressiveDirection.HIGHER,
                            lenient_multiplier=Decimal(1),
                            deviation_mode=DeviationMode.ABSOLUTE,
                        ),
                    ),
                ),
            ),
            half_life_rounds=2,
        )
        service = _validator_service(
            storage,
            now_holder,
            universe_provider=StaticLendingUniverseProvider(netuids=(44,)),
            lending_orchestrator=assessment,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            horizons=(5, 30),
            sessions=(SESSIONS[0],),
        )
        round_id = "2023-03-06"
        for horizon in (5, 30):
            storage.upsert_assessment_ema(
                FORGE_LENDING_SCHEMA_ID,
                AssessmentEmaState(
                    miner_hotkey="hk-gone",
                    coordinate=coordinate(horizon),
                    ema=Decimal(1),
                    resolved_rounds=1,
                ),
                now_iso=now_holder["now"].isoformat(),
            )

        service.tick(expected_miners=())
        windows = storage.round_windows(round_id, FORGE_LENDING_SCHEMA_ID)
        assert windows is not None
        _record_accepted_submission(
            storage,
            round_id,
            "hk-gone",
            now_iso=now_holder["now"].isoformat(),
        )
        now_holder["now"] = windows.reveal_close + timedelta(seconds=6)
        storage.set_round_state(
            round_id,
            FORGE_LENDING_SCHEMA_ID,
            "revealed",
            now_iso=now_holder["now"].isoformat(),
        )
        service.tick(expected_miners=(), archive_hotkeys=("hk-gone",))

        first_history = storage.assessment_score_history_for_round(
            round_id, FORGE_LENDING_SCHEMA_ID
        )
        assert [row.coordinate.horizon_value for row in first_history] == [5]
        assert all(row.round_score == Decimal(0) for row in first_history)
        assert [
            state.coordinate.horizon_value
            for state in storage.assessment_ema_states(FORGE_LENDING_SCHEMA_ID)
        ] == [30]

        now_holder["now"] = windows.reveal_close + timedelta(seconds=31)
        service.tick(expected_miners=(), archive_hotkeys=("hk-gone",))

        history = storage.assessment_score_history_for_round(
            round_id, FORGE_LENDING_SCHEMA_ID
        )
        assert [row.coordinate.horizon_value for row in history] == [5, 30]
        assert all(row.round_score == Decimal(0) for row in history)
        assert storage.assessment_ema_states(FORGE_LENDING_SCHEMA_ID) == []

    def test_archival_zero_fills_same_horizon_across_rounds_before_deleting_ema_state(
        self, storage: Storage
    ) -> None:
        def coordinate(horizon: int) -> AssessmentCoordinate:
            return AssessmentCoordinate.subnet_asset(
                netuid=44, horizon_seconds=horizon, output="alpha"
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

        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        assessment = AssessmentScoringOrchestrator(
            storage=storage,
            config=AssessmentScoringConfig(
                schema_id=FORGE_LENDING_SCHEMA_ID,
                horizons=(5,),
                universe_members=lambda tickers: tuple(
                    int(ticker) for ticker in tickers
                ),
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
                        spec=_AssessmentSpec(
                            grace_band=0,
                            cutoff=100,
                            aggressive_direction=AggressiveDirection.HIGHER,
                            lenient_multiplier=Decimal(1),
                            deviation_mode=DeviationMode.ABSOLUTE,
                        ),
                    ),
                ),
            ),
            half_life_rounds=2,
        )
        service = _validator_service(
            storage,
            now_holder,
            universe_provider=StaticLendingUniverseProvider(netuids=(44,)),
            lending_orchestrator=assessment,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            horizons=(5,),
            sessions=(SESSIONS[0],),
        )
        old_round = "2023-03-06"
        new_round = "2023-03-07"

        service.tick(expected_miners=())
        old_windows = storage.round_windows(old_round, FORGE_LENDING_SCHEMA_ID)
        old_universe = storage.universe_for(old_round, FORGE_LENDING_SCHEMA_ID)
        assert old_windows is not None
        assert old_universe is not None
        new_windows = replace(
            old_windows,
            round_id=new_round,
            reveal_close=old_windows.reveal_close + timedelta(seconds=25),
        )
        storage.open_round(
            windows=new_windows,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            universe=replace(old_universe, round_id=new_round),
            now_iso=now_holder["now"].isoformat(),
        )
        for miner_hotkey in ("hk-active", "hk-gone"):
            _record_accepted_submission(
                storage,
                old_round,
                miner_hotkey,
                now_iso=now_holder["now"].isoformat(),
            )
            storage.upsert_assessment_ema(
                FORGE_LENDING_SCHEMA_ID,
                AssessmentEmaState(
                    miner_hotkey=miner_hotkey,
                    coordinate=coordinate(5),
                    ema=Decimal(1),
                    resolved_rounds=1,
                ),
                now_iso=now_holder["now"].isoformat(),
            )
        now_holder["now"] = old_windows.reveal_close + timedelta(seconds=31)
        storage.set_round_state(
            old_round,
            FORGE_LENDING_SCHEMA_ID,
            "revealed",
            now_iso=now_holder["now"].isoformat(),
        )
        storage.set_round_state(
            new_round,
            FORGE_LENDING_SCHEMA_ID,
            "revealed",
            now_iso=now_holder["now"].isoformat(),
        )
        # When: one catch-up tick resolves two unfinished rounds sharing a due horizon.
        service.tick(expected_miners=(), archive_hotkeys=("hk-gone",))

        assert [
            row.coordinate.horizon_value
            for row in storage.assessment_score_history_for_round(
                old_round, FORGE_LENDING_SCHEMA_ID
            )
        ] == [5, 5]
        assert [
            row.coordinate.horizon_value
            for row in storage.assessment_score_history_for_round(
                new_round, FORGE_LENDING_SCHEMA_ID
            )
        ] == [5, 5]
        assert [
            state.miner_hotkey
            for state in storage.assessment_ema_states(FORGE_LENDING_SCHEMA_ID)
        ] == ["hk-active"]

    def test_archival_failure_keeps_scored_weights_and_retries_next_tick(
        self, storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Assessment:
            def resolve_and_score(
                self,
                round_id: str,
                horizon: int,
                *,
                now_iso: str,
                resolution_due_at: datetime | None = None,
                archive_hotkeys: Sequence[str] = (),
            ) -> dict[str, Decimal]:
                storage.record_assessment_scoring_pass(
                    round_id,
                    FORGE_LENDING_SCHEMA_ID,
                    horizon_value=horizon,
                    realized_targets=(),
                    output_scores=(),
                    ema_updates=(),
                    score_history=(),
                    now_iso=now_iso,
                )
                return {"hk-active": Decimal(1)}

            def weights(self) -> dict[str, Decimal]:
                return {"hk-active": Decimal(1)}

            def blended_scores(self) -> dict[str, Decimal]:
                return {"hk-active": Decimal(1)}

        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        service = _validator_service(
            storage,
            now_holder,
            universe_provider=StaticLendingUniverseProvider(netuids=(44,)),
            lending_orchestrator=_Assessment(),
            schema_id=FORGE_LENDING_SCHEMA_ID,
            horizons=(5,),
            sessions=(SESSIONS[0],),
        )
        storage.upsert_assessment_ema(
            FORGE_LENDING_SCHEMA_ID,
            AssessmentEmaState(
                miner_hotkey="hk-gone",
                coordinate=AssessmentCoordinate.subnet_asset(
                    netuid=44, horizon_seconds=5, output="alpha"
                ),
                ema=Decimal(1),
                resolved_rounds=1,
            ),
            now_iso=now_holder["now"].isoformat(),
        )
        archive = storage.archive_assessment_ema_horizon
        calls = 0

        def fail_once(schema_id: str, horizon: int, hotkeys: Sequence[str]) -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("archive unavailable")
            return archive(schema_id, horizon, hotkeys)

        monkeypatch.setattr(storage, "archive_assessment_ema_horizon", fail_once)
        service.tick(expected_miners=())
        now_holder["now"] = EPOCH + timedelta(seconds=PERIOD + 6)

        assert service.tick(expected_miners=(), archive_hotkeys=("hk-gone",)) == {
            "hk-active": Decimal(1)
        }
        assert storage.assessment_ema_states(FORGE_LENDING_SCHEMA_ID)

        assert service.tick(expected_miners=(), archive_hotkeys=("hk-gone",)) == {
            "hk-active": Decimal(1)
        }

        assert calls == 2
        assert storage.assessment_ema_states(FORGE_LENDING_SCHEMA_ID) == []

    def test_incomplete_assessment_pass_keeps_ema_until_marker_is_recorded(
        self, storage: Storage
    ) -> None:
        class _PartialThenCompleteAssessment:
            def __init__(self) -> None:
                self.calls = 0

            def resolve_and_score(
                self,
                round_id: str,
                horizon: int,
                *,
                now_iso: str,
                resolution_due_at: datetime | None = None,
                archive_hotkeys: Sequence[str] = (),
            ) -> dict[str, Decimal]:
                self.calls += 1
                storage.record_assessment_scoring_pass(
                    round_id,
                    FORGE_LENDING_SCHEMA_ID,
                    horizon_value=horizon,
                    realized_targets=(),
                    output_scores=(),
                    ema_updates=(
                        AssessmentEmaState(
                            miner_hotkey="hk-gone",
                            coordinate=AssessmentCoordinate.subnet_asset(
                                netuid=44, horizon_seconds=horizon, output="alpha"
                            ),
                            ema=Decimal(0),
                            resolved_rounds=self.calls,
                        ),
                    ),
                    score_history=(),
                    complete=self.calls > 1,
                    now_iso=now_iso,
                )
                return {"hk-active": Decimal(1)}

            def weights(self) -> dict[str, Decimal]:
                return {"hk-active": Decimal(1)}

            def blended_scores(self) -> dict[str, Decimal]:
                return {"hk-active": Decimal(1)}

        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        assessment = _PartialThenCompleteAssessment()
        service = _validator_service(
            storage,
            now_holder,
            universe_provider=StaticLendingUniverseProvider(netuids=(44,)),
            lending_orchestrator=assessment,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            horizons=(5,),
            sessions=(SESSIONS[0],),
        )
        service.tick(expected_miners=())
        now_holder["now"] = EPOCH + timedelta(seconds=PERIOD + 6)

        service.tick(expected_miners=(), archive_hotkeys=("hk-gone",))

        assert not storage.has_assessment_resolution_marker(
            "2023-03-06", FORGE_LENDING_SCHEMA_ID, 5
        )
        assert storage.assessment_ema_states(FORGE_LENDING_SCHEMA_ID)

        service.tick(expected_miners=(), archive_hotkeys=("hk-gone",))

        assert assessment.calls == 2
        assert storage.has_assessment_resolution_marker(
            "2023-03-06", FORGE_LENDING_SCHEMA_ID, 5
        )
        assert storage.assessment_ema_states(FORGE_LENDING_SCHEMA_ID) == []

    def test_empty_assessment_universe_closes_after_due_horizon(
        self, storage: Storage
    ) -> None:
        class _EmptyUniverseAssessment:
            def __init__(self) -> None:
                self.calls: list[int] = []

            def resolve_and_score(
                self,
                round_id: str,
                horizon: int,
                *,
                now_iso: str,
                resolution_due_at: datetime | None = None,
                archive_hotkeys: Sequence[str] = (),
            ) -> dict[str, Decimal]:
                self.calls.append(horizon)
                storage.record_assessment_scoring_pass(
                    round_id,
                    FORGE_LENDING_SCHEMA_ID,
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

        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        assessment = _EmptyUniverseAssessment()
        service = _validator_service(
            storage,
            now_holder,
            universe_provider=StaticLendingUniverseProvider(netuids=()),
            lending_orchestrator=assessment,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            horizons=(5,),
        )
        round_id = "2023-03-06"

        service.tick(expected_miners=())
        now_holder["now"] = EPOCH + timedelta(seconds=PERIOD + 6)
        service.tick(expected_miners=())

        assert assessment.calls == [5]
        assert storage.has_assessment_resolution_marker(
            round_id, FORGE_LENDING_SCHEMA_ID, 5
        )
        assert storage.round_state(round_id, FORGE_LENDING_SCHEMA_ID) == "closed"

    def test_assessment_failure_before_persist_does_not_mark_horizon_resolved(
        self, storage: Storage
    ) -> None:
        class _FailingAssessment:
            def resolve_and_score(
                self,
                round_id: str,
                horizon: int,
                *,
                now_iso: str,
                resolution_due_at: datetime | None = None,
                archive_hotkeys: Sequence[str] = (),
            ) -> dict[str, Decimal]:
                del resolution_due_at
                raise RuntimeError("live provider head unavailable")

            def weights(self) -> dict[str, Decimal]:
                return {}

            def blended_scores(self) -> dict[str, Decimal]:
                return {}

        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        service = _validator_service(
            storage,
            now_holder,
            universe_provider=StaticLendingUniverseProvider(netuids=(44,)),
            lending_orchestrator=_FailingAssessment(),
            schema_id=FORGE_LENDING_SCHEMA_ID,
            horizons=(5,),
        )
        round_id = "2023-03-06"

        service.tick(expected_miners=())
        now_holder["now"] = EPOCH + timedelta(seconds=PERIOD + 6)
        service.tick(expected_miners=())

        assert not storage.has_assessment_resolution_marker(
            round_id, FORGE_LENDING_SCHEMA_ID, 5
        )
        assert storage.round_state(round_id, FORGE_LENDING_SCHEMA_ID) == "revealed"

    def test_lending_round_rejects_universe_over_fanout_cap(
        self, storage: Storage
    ) -> None:
        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        service = _validator_service(
            storage,
            now_holder,
            universe_provider=StaticLendingUniverseProvider(netuids=(8, 44)),
            schema_id=FORGE_LENDING_SCHEMA_ID,
            max_universe_targets=1,
        )

        service.tick(expected_miners=("hk-a",))

        assert storage.round_state("2023-03-06", FORGE_LENDING_SCHEMA_ID) is None
        assert service.consecutive_universe_failures == 1
        assert service.last_universe_error == "ValueError"

    def test_zero_submission_round_reveals_without_consensus(
        self, storage: Storage
    ) -> None:
        """No miner showed up: the round still advances cleanly — revealed,
        no consensus rows, no crash (review test-gap)."""
        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        service = _validator_service(storage, now_holder)
        service.tick(expected_miners=())

        now_holder["now"] = EPOCH + timedelta(seconds=110)
        service.tick(expected_miners=())

        assert storage.round_state("2023-03-06", FORGE_LENDING_SCHEMA_ID) == "revealed"
        assert (
            storage.assessment_consensus_for("2023-03-06", FORGE_LENDING_SCHEMA_ID)
            == []
        )

    def test_resumes_from_storage_after_restart(self, storage: Storage) -> None:
        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        _validator_service(storage, now_holder).tick(expected_miners=("hk-a",))

        # A fresh service instance (crash + restart) carries on from the DB.
        now_holder["now"] = EPOCH + timedelta(seconds=210)
        rebuilt = _validator_service(storage, now_holder)
        weights = rebuilt.tick(expected_miners=("hk-a",))

        assert storage.round_state("2023-03-06", FORGE_LENDING_SCHEMA_ID) == "closed"
        assert weights is not None


def _miner_service(
    sent: list[object],
    now_holder: dict[str, datetime],
    *,
    accept: dict[str, int] | None = None,
    state_path: Path | None = None,
    serving: int = 1,
) -> MinerRoundService:
    """Reference-miner service over the synthetic scheduler.

    The send seam models the neuron's dedup: it pushes only to validators that
    have not yet acked this (round, synapse-type), records one push in ``sent``
    when it actually pushes, and returns the cumulative count holding it.
    ``accept['n']`` validators ack per push (capped by those still missing).
    """
    acceptances = accept if accept is not None else {"n": 1}
    held: dict[tuple[str, str], set[int]] = {}

    async def send(synapse: SubmitCommit | SubmitReveal) -> int:
        key = (synapse.round_id, type(synapse).__name__)
        holders = held.setdefault(key, set())
        missing = [
            validator for validator in range(serving) if validator not in holders
        ]
        if missing:
            sent.append(synapse)
            for validator in missing[: acceptances["n"]]:
                holders.add(validator)
        return len(holders)

    return MinerRoundService(
        scheduler=SyntheticScheduler(
            sessions=SESSIONS, epoch=EPOCH, period_seconds=PERIOD
        ),
        assemble=LendingBaselineAssembler(
            netuids=(44,),
            miner_hotkey="hk-miner",
        ),
        send=send,
        now_fn=lambda: now_holder["now"],
        state_path=state_path,
    )


class TestMinerRoundService:
    async def test_commits_then_reveals_once(self) -> None:
        sent: list[object] = []
        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        service = _miner_service(sent, now_holder)

        await service.tick()  # commit window → SubmitCommit
        await service.tick()  # idempotent
        now_holder["now"] = EPOCH + timedelta(seconds=60)
        await service.tick()  # reveal window → SubmitReveal
        await service.tick()  # idempotent

        assert len(sent) == 2
        commit, reveal = sent
        assert isinstance(commit, SubmitCommit)
        assert isinstance(reveal, SubmitReveal)
        assert commit.round_id == reveal.round_id == "2023-03-06"
        assert reveal.bundle_json

    async def test_unacknowledged_commit_retries_until_accepted(self) -> None:
        """Zero validator acceptances must not burn the round: the commit is
        retried each tick until at least one validator holds it."""
        sent: list[object] = []
        accept = {"n": 0}
        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        service = _miner_service(sent, now_holder, accept=accept)

        await service.tick()  # 0/N accepted → not committed
        await service.tick()  # retry
        accept["n"] = 1
        await service.tick()  # accepted → committed
        await service.tick()  # idempotent now

        commits = [s for s in sent if isinstance(s, SubmitCommit)]
        assert len(commits) == 3
        assert len({c.bundle_hash for c in commits}) == 1  # same assembled bundle

    async def test_commit_keeps_pushing_until_all_serving_validators_ack(
        self,
    ) -> None:
        """With multiple validators, one early ack must not stop distribution:
        the commit is re-pushed each tick (the seam targets only the still-
        missing validators) until every serving validator holds it."""
        sent: list[object] = []
        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        service = _miner_service(sent, now_holder, serving=2, accept={"n": 1})

        await service.tick()  # commit pushed; validator 0 acks
        await service.tick()  # re-push reaches validator 1
        await service.tick()  # both hold it → no further push

        commits = [s for s in sent if isinstance(s, SubmitCommit)]
        assert len(commits) == 2

    async def test_unacknowledged_reveal_retries_until_accepted(self) -> None:
        sent: list[object] = []
        accept = {"n": 1}
        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        service = _miner_service(sent, now_holder, accept=accept)
        await service.tick()  # committed

        now_holder["now"] = EPOCH + timedelta(seconds=60)
        accept["n"] = 0
        await service.tick()  # reveal 0/N accepted → retry next tick
        await service.tick()
        accept["n"] = 1
        await service.tick()  # accepted → revealed
        await service.tick()  # idempotent

        reveals = [s for s in sent if isinstance(s, SubmitReveal)]
        assert len(reveals) == 3

    async def test_assembly_runs_off_the_event_loop_thread(self) -> None:
        """Blocking live fetches during assembly must run off the event loop
        so the miner's axon/dendrite coroutines keep serving (review M5)."""
        import threading

        loop_thread = threading.get_ident()
        fetch_threads: list[int] = []
        inner = LendingBaselineAssembler(netuids=(44,), miner_hotkey="hk-miner")

        class _ThreadSpy:
            @property
            def schema_id(self) -> str:
                return inner.schema_id

            def __call__(self, round_id: str) -> AssembledSubmission:
                fetch_threads.append(threading.get_ident())
                return inner(round_id)

        sent: list[object] = []
        now_holder = {"now": EPOCH + timedelta(seconds=10)}

        async def send(synapse: SubmitCommit | SubmitReveal) -> int:
            sent.append(synapse)
            return 1

        service = MinerRoundService(
            scheduler=SyntheticScheduler(
                sessions=SESSIONS, epoch=EPOCH, period_seconds=PERIOD
            ),
            assemble=_ThreadSpy(),
            send=send,
            now_fn=lambda: now_holder["now"],
            state_path=None,
        )

        await service.tick()

        assert fetch_threads, "assembly should invoke the assembler"
        assert all(thread != loop_thread for thread in fetch_threads)

    async def test_state_file_is_owner_only(self, tmp_path: Path) -> None:
        """The state file holds the pre-reveal bundle + nonce — it must not
        be world/group readable (commit-reveal confidentiality at rest)."""
        state_path = tmp_path / "miner_state.json"
        sent: list[object] = []
        now_holder = {"now": EPOCH + timedelta(seconds=10)}

        await _miner_service(sent, now_holder, state_path=state_path).tick()

        assert state_path.exists()
        assert (state_path.stat().st_mode & 0o077) == 0  # no group/other bits

    async def test_corrupt_state_file_does_not_brick_the_miner(
        self, tmp_path: Path
    ) -> None:
        """Resilience layer must never be the thing that stops the miner:
        a corrupt/truncated state file is set aside and mining continues."""
        state_path = tmp_path / "miner_state.json"
        state_path.write_text('{"2026-06-09": {"bundle_json": tru', encoding="utf-8")
        sent: list[object] = []
        now_holder = {"now": EPOCH + timedelta(seconds=10)}

        service = _miner_service(sent, now_holder, state_path=state_path)
        await service.tick()

        assert len(sent) == 1  # fresh state, mining proceeds
        quarantined = list(tmp_path.glob("miner_state.corrupt-*"))
        assert len(quarantined) == 1

    async def test_repeated_corruption_keeps_each_quarantine(
        self, tmp_path: Path
    ) -> None:
        """A second corruption must not overwrite the first quarantine —
        forensic copies are uniquified, not clobbered."""
        state_path = tmp_path / "miner_state.json"
        now_holder = {"now": EPOCH + timedelta(seconds=10)}

        state_path.write_text("{bad", encoding="utf-8")
        _miner_service([], now_holder, state_path=state_path)
        state_path.write_text("{bad again", encoding="utf-8")
        _miner_service([], now_holder, state_path=state_path)

        quarantined = list(tmp_path.glob("miner_state.corrupt-*"))
        assert len(quarantined) == 2

    async def test_restart_between_commit_and_reveal_recovers_nonce(
        self, tmp_path: Path
    ) -> None:
        """A miner restart in the commit→reveal gap must not burn the round:
        the nonce is persisted, so the fresh process reveals the exact
        preimage the validators hold a commit for."""
        state_path = tmp_path / "miner_state.json"
        sent: list[object] = []
        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        await _miner_service(sent, now_holder, state_path=state_path).tick()
        [commit] = sent
        assert isinstance(commit, SubmitCommit)

        # Crash + restart: a brand-new process with only the state file.
        now_holder["now"] = EPOCH + timedelta(seconds=60)
        rebuilt = _miner_service(sent, now_holder, state_path=state_path)
        await rebuilt.tick()

        reveal = sent[-1]
        assert isinstance(reveal, SubmitReveal)
        recomputed = commit_hash(
            reveal.bundle_json.encode(),
            bytes.fromhex(reveal.nonce_hex),
            miner_hotkey="hk-miner",
        )
        assert recomputed == commit.bundle_hash
