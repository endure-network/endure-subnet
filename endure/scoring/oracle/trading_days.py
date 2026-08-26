"""NYSE trading-day arithmetic (spec §2, §7).

Thin wrapper over ``exchange-calendars``' XNYS calendar. Session boundaries
are protocol-relevant — every validator must agree on which day is t0+H — so
the dependency version is pinned and drift is guarded by a test that asserts
sessions exist well into the future.
"""

from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache

import exchange_calendars as xcals


@lru_cache(maxsize=1)
def _calendar() -> xcals.ExchangeCalendar:
    return xcals.get_calendar("XNYS")


def is_trading_session(day: date) -> bool:
    """True when the NYSE has a session on ``day``."""
    return bool(_calendar().is_session(day.isoformat()))


def trading_sessions(start: date, end: date) -> tuple[date, ...]:
    """All NYSE sessions in ``[start, end]``, ascending."""
    sessions = _calendar().sessions_in_range(start.isoformat(), end.isoformat())
    return tuple(session.date() for session in sessions)


def session_close(session: date) -> datetime:
    """The official close of an NYSE session (tz-aware, handles half-days)."""
    if not is_trading_session(session):
        raise ValueError(f"{session} is not an NYSE session")
    close = _calendar().session_close(session.isoformat())
    return close.to_pydatetime()


def add_trading_days(session: date, count: int) -> date:
    """The session ``count`` trading days after ``session``.

    ``session`` must itself be a trading session; ``count`` of 0 returns it.
    """
    if count < 0:
        raise ValueError("count must be non-negative")
    if not is_trading_session(session):
        raise ValueError(f"{session} is not an NYSE session")
    if count == 0:
        return session
    calendar = _calendar()
    target = calendar.session_offset(session.isoformat(), count)
    return target.date()
