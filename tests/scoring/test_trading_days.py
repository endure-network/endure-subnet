"""NYSE trading-day arithmetic (spec §2, §7)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from endure.scoring.oracle.trading_days import (
    add_trading_days,
    is_trading_session,
    trading_sessions,
)


class TestIsTradingSession:
    def test_weekday_session(self) -> None:
        assert is_trading_session(date(2023, 3, 10)) is True

    def test_weekend_is_not_a_session(self) -> None:
        assert is_trading_session(date(2023, 3, 11)) is False

    def test_good_friday_holiday_is_not_a_session(self) -> None:
        assert is_trading_session(date(2023, 4, 7)) is False


class TestAddTradingDays:
    def test_skips_weekend(self) -> None:
        assert add_trading_days(date(2023, 3, 10), 1) == date(2023, 3, 13)

    def test_skips_holiday(self) -> None:
        assert add_trading_days(date(2023, 4, 6), 1) == date(2023, 4, 10)

    def test_zero_returns_same_session(self) -> None:
        assert add_trading_days(date(2023, 3, 10), 0) == date(2023, 3, 10)

    def test_five_trading_days_spans_a_week(self) -> None:
        assert add_trading_days(date(2023, 3, 6), 5) == date(2023, 3, 13)

    def test_rejects_non_session_start(self) -> None:
        with pytest.raises(ValueError):
            add_trading_days(date(2023, 3, 11), 1)


class TestTradingSessions:
    def test_inclusive_session_range(self) -> None:
        sessions = trading_sessions(date(2023, 3, 6), date(2023, 3, 10))

        assert sessions == (
            date(2023, 3, 6),
            date(2023, 3, 7),
            date(2023, 3, 8),
            date(2023, 3, 9),
            date(2023, 3, 10),
        )


class TestCalendarDrift:
    def test_calendar_provides_sessions_six_months_ahead(self) -> None:
        # Guardrail from design review: a stale calendar dependency computes
        # wrong session boundaries, which is protocol-level breakage.
        horizon_end = date.today() + timedelta(days=180)

        sessions = trading_sessions(date.today(), horizon_end)

        assert len(sessions) > 100
