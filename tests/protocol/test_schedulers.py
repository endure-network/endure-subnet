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

    def test_resolution_due_after_horizon_periods(self) -> None:
        scheduler = SyntheticScheduler(
            sessions=SESSIONS, epoch=EPOCH, period_seconds=100
        )

        # Round 0 (2023-03-06), horizon 1: resolves once period 1 fully closes.
        not_yet = EPOCH + timedelta(seconds=150)
        due = EPOCH + timedelta(seconds=210)

        assert scheduler.resolution_due("2023-03-06", 1, not_yet) is False
        assert scheduler.resolution_due("2023-03-06", 1, due) is True

    def test_resolution_never_due_beyond_fixture_sessions(self) -> None:
        scheduler = SyntheticScheduler(
            sessions=SESSIONS, epoch=EPOCH, period_seconds=100
        )

        far_future = EPOCH + timedelta(seconds=100 * 50)

        assert scheduler.resolution_due("2023-03-09", 5, far_future) is False


class TestNyseScheduler:
    def test_active_window_on_a_session_day(self) -> None:
        scheduler = NyseScheduler(fetch_delay_seconds=72000)

        # 2026-06-09 15:00 UTC is inside the default commit window.
        window = scheduler.active_window(datetime(2026, 6, 9, 15, 0, tzinfo=UTC))

        assert window is not None
        assert window.round_id == "2026-06-09"

    def test_no_window_on_weekends(self) -> None:
        scheduler = NyseScheduler(fetch_delay_seconds=72000)

        assert scheduler.active_window(datetime(2026, 6, 7, 15, 0, tzinfo=UTC)) is None

    def test_publication_waits_for_the_next_session_commit_close(self) -> None:
        scheduler = NyseScheduler(fetch_delay_seconds=72000)
        window = scheduler.active_window(datetime(2026, 6, 9, 15, 0, tzinfo=UTC))

        assert window is not None
        assert scheduler.publication_available_at(window) == datetime(
            2026, 6, 10, 19, 30, tzinfo=UTC
        )

    def test_resolution_due_after_fetch_delay(self) -> None:
        scheduler = NyseScheduler(fetch_delay_seconds=72000)

        # Round 2026-06-09, horizon 1 resolves at 2026-06-10 close (20:00 UTC);
        # fetch-ready 20 hours later: 2026-06-11 16:00 UTC.
        assert (
            scheduler.resolution_due(
                "2026-06-09", 1, datetime(2026, 6, 11, 15, 0, tzinfo=UTC)
            )
            is False
        )
        assert (
            scheduler.resolution_due(
                "2026-06-09", 1, datetime(2026, 6, 11, 17, 0, tzinfo=UTC)
            )
            is True
        )
