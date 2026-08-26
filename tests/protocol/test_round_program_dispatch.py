from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

from endure.assessment.registry import UniverseSnapshot
from endure.protocol.round_engine import RoundWindows
from endure.protocol.schedulers import SyntheticScheduler
from endure.protocol.validator_service import ValidatorRoundService
from endure.protocol.vertical import RoundProgram
from endure.storage.repository import Storage

EPOCH = datetime(2026, 8, 12, tzinfo=UTC)


class _EmptyUniverseProvider:
    def fetch_universe(self, round_id: str) -> UniverseSnapshot:
        return UniverseSnapshot(round_id=round_id, tickers=(), source_hash="empty")


class _StubRoundProgram:
    def __init__(self, *, had_submissions: bool = False) -> None:
        self.had_submissions = had_submissions
        self.published: list[tuple[str, datetime]] = []

    def weights(self) -> dict[str, Decimal]:
        return {"hk-a": Decimal("0.9")}

    def blended_scores(self) -> dict[str, Decimal]:
        return {"hk-a": Decimal("0.5")}

    def publish_consensus(self, round_id: str, now: datetime) -> bool:
        self.published.append((round_id, now))
        return self.had_submissions

    def resolve_due(
        self,
        round_id: str,
        windows: RoundWindows,
        now: datetime,
        expected_miners: Sequence[str],
    ) -> tuple[bool, str | None]:
        del round_id, windows, now, expected_miners
        return False, None


def _service(
    storage: Storage,
    *,
    round_program: RoundProgram,
) -> ValidatorRoundService:
    return ValidatorRoundService(
        storage=storage,
        scheduler=SyntheticScheduler(
            sessions=(date(2026, 8, 12),), epoch=EPOCH, period_seconds=60
        ),
        universe_provider=_EmptyUniverseProvider(),
        schema_id="risk.v1.subnet_alpha",
        horizons=(1,),
        now_fn=lambda: EPOCH,
        round_program=round_program,
    )


def test_blended_snapshot_routes_to_round_program(storage: Storage) -> None:
    service = _service(storage, round_program=_StubRoundProgram())

    assert service.blended_snapshot() == {"hk-a": Decimal("0.5")}


def test_tick_routes_final_weights_to_round_program(
    storage: Storage, monkeypatch
) -> None:
    service = _service(storage, round_program=_StubRoundProgram())
    monkeypatch.setattr(service, "_open_active_round", lambda _now: None)
    monkeypatch.setattr(
        service,
        "_advance_rounds",
        lambda _now, _expected, _archive: True,
    )

    assert service.tick(expected_miners=()) == {"hk-a": Decimal("0.9")}


def test_constructor_routes_legacy_score_adapter_through_program(
    storage: Storage,
) -> None:
    class _LegacyRoundProgram(_StubRoundProgram):
        def weights(self) -> dict[str, Decimal]:
            return {"hk-a": Decimal("0.7")}

        def blended_scores(self) -> dict[str, Decimal]:
            return {"hk-a": Decimal("0.4")}

    service = _service(storage, round_program=_LegacyRoundProgram())

    assert service.blended_snapshot() == {"hk-a": Decimal("0.4")}


def test_publish_consensus_routes_to_round_program_and_records_submissions(
    storage: Storage,
) -> None:
    # Given: a vertical program that reports accepted submissions for the round.
    program = _StubRoundProgram(had_submissions=True)
    service = _service(storage, round_program=program)
    service._empty_scored_rounds = 2
    service._last_empty_round = "previous-round"

    # When: reveal-close consensus dispatch runs through the service.
    service._publish_consensus_and_reveal("2026-08-12", EPOCH)

    # Then: the program owns publication and its boolean resets service health state.
    assert program.published == [("2026-08-12", EPOCH)]
    assert service.consecutive_empty_scored_rounds == 0
    assert service.last_empty_scored_round is None
