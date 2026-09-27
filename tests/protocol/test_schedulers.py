"""Round schedulers: NYSE-anchored and synthetic/compressed (spec §2)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from endure.protocol.schedulers import NyseScheduler, SyntheticScheduler

SESSIONS = (date(2023, 3, 6), date(2023, 3, 7), date(2023, 3, 8), date(2023, 3, 9))
EPOCH = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)


class TestSyntheticScheduler:
    def test_maps_wall_clock_periods_onto_fixture_sessions(self) -> None:
        scheduler = SyntheticScheduler(
            sessions=SESSIONS, epoch=EPOCH, period_seconds=100
        )

        window = scheduler.active_window(EPOCH + timedelta(seconds=10))

        assert window is not None
        assert window.round_id == "2023-03-06"
        assert window.commit_close < window.t0_close < window.reveal_open

    def test_second_period_is_the_next_session(self) -> None:
        scheduler = SyntheticScheduler(
            sessions=SESSIONS, epoch=EPOCH, period_seconds=100
        )

        window = scheduler.active_window(EPOCH + timedelta(seconds=150))

        assert window is not None
        assert window.round_id == "2023-03-07"

    def test_publication_waits_for_the_next_period_commit_close(self) -> None:
        scheduler = SyntheticScheduler(
            sessions=SESSIONS, epoch=EPOCH, period_seconds=100
        )
        window = scheduler.active_window(EPOCH + timedelta(seconds=10))

        assert window is not None
        assert scheduler.publication_available_at(window) == EPOCH + timedelta(
            seconds=140
        )

    def test_no_window_before_epoch_or_after_sessions_exhaust(self) -> None:
        scheduler = SyntheticScheduler(
            sessions=SESSIONS, epoch=EPOCH, period_seconds=100
        )

        assert scheduler.active_window(EPOCH - timedelta(seconds=1)) is None
        assert scheduler.active_window(EPOCH + timedelta(seconds=100 * 10)) is None


class TestNyseScheduler:
    def test_active_window_on_a_session_day(self) -> None:
        scheduler = NyseScheduler()

        # 2026-06-09 15:00 UTC is inside the default commit window.
        window = scheduler.active_window(datetime(2026, 6, 9, 15, 0, tzinfo=UTC))

        assert window is not None
        assert window.round_id == "2026-06-09"

    def test_no_window_on_weekends(self) -> None:
        scheduler = NyseScheduler()

        assert scheduler.active_window(datetime(2026, 6, 7, 15, 0, tzinfo=UTC)) is None

    def test_publication_waits_for_the_next_session_commit_close(self) -> None:
        scheduler = NyseScheduler()
        window = scheduler.active_window(datetime(2026, 6, 9, 15, 0, tzinfo=UTC))

        assert window is not None
        assert scheduler.publication_available_at(window) == datetime(
            2026, 6, 10, 19, 30, tzinfo=UTC
        )
