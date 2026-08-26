"""Round windows + phase machine (spec §2) — pure and clock-free."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from endure.protocol.round_engine import (
    DEFAULT_OFFSETS,
    RoundPhase,
    WindowOffsets,
    compute_windows,
    phase_at,
)


class TestComputeWindows:
    def test_regular_day_windows_are_close_relative(self) -> None:
        # 2026-06-09 is a regular NYSE session: close 16:00 ET = 20:00 UTC (EDT).
        windows = compute_windows(date(2026, 6, 9), offsets=DEFAULT_OFFSETS)

        close = datetime(2026, 6, 9, 20, 0, tzinfo=UTC)
        assert windows.t0_close == close
        assert windows.commit_close == close - timedelta(minutes=30)
        assert windows.reveal_open == close + timedelta(minutes=30)
        assert windows.reveal_close == close + timedelta(hours=4)
        assert windows.commit_open == close - timedelta(hours=9)
        assert windows.round_id == "2026-06-09"

    def test_half_day_windows_shift_with_the_early_close(self) -> None:
        # Black Friday 2026-11-27 closes 13:00 ET = 18:00 UTC (EST).
        windows = compute_windows(date(2026, 11, 27), offsets=DEFAULT_OFFSETS)

        assert windows.t0_close == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
        assert windows.commit_close == windows.t0_close - timedelta(minutes=30)

    def test_compressed_offsets_for_localnet(self) -> None:
        fast = WindowOffsets(
            commit_open_before_close=timedelta(minutes=5),
            commit_close_before_close=timedelta(minutes=2),
            reveal_open_after_close=timedelta(minutes=1),
            reveal_close_after_close=timedelta(minutes=3),
        )

        windows = compute_windows(date(2026, 6, 9), offsets=fast)

        assert windows.reveal_close - windows.commit_open == timedelta(minutes=8)

    def test_non_session_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_windows(date(2026, 6, 7), offsets=DEFAULT_OFFSETS)  # Sunday


class TestPhaseAt:
    @pytest.fixture
    def windows(self):
        return compute_windows(date(2026, 6, 9), offsets=DEFAULT_OFFSETS)

    def test_phase_progression(self, windows) -> None:
        close = windows.t0_close

        assert phase_at(close - timedelta(hours=10), windows) is RoundPhase.PRE_OPEN
        assert phase_at(close - timedelta(hours=2), windows) is RoundPhase.COMMIT
        assert (
            phase_at(close - timedelta(minutes=10), windows)
            is RoundPhase.AWAITING_CLOSE
        )
        assert phase_at(close + timedelta(hours=1), windows) is RoundPhase.REVEAL
        assert phase_at(close + timedelta(hours=5), windows) is RoundPhase.CLOSED

    def test_boundaries_are_inclusive_for_open_exclusive_for_close(
        self, windows
    ) -> None:
        assert phase_at(windows.commit_open, windows) is RoundPhase.COMMIT
        assert phase_at(windows.commit_close, windows) is RoundPhase.COMMIT
        assert phase_at(windows.reveal_close, windows) is RoundPhase.REVEAL
