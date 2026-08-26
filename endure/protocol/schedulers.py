"""Round schedulers (spec §2; 24/7 rounds spec).

Three interchangeable clocks behind one seam: ``NyseScheduler`` anchors rounds
to real NYSE sessions with close-relative offsets and a settled-data fetch
delay; ``FixedUtcScheduler`` anchors Alpha Risk rounds at 20:00 UTC every
calendar day; and ``SyntheticScheduler`` maps wall-clock periods onto fixture
sessions so localnet and mock runs compress a full multi-day loop into minutes
while the scoring math runs on real historical data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from endure.assessment.registry import default_registry
from endure.protocol.round_engine import (
    DEFAULT_OFFSETS,
    RoundWindows,
    WindowOffsets,
    compute_fixed_utc_windows,
    compute_windows,
)
from endure.scoring.oracle.trading_days import (
    add_trading_days,
    is_trading_session,
    session_close,
)

_NEW_YORK = ZoneInfo("America/New_York")


class RoundScheduler(Protocol):
    """Seam between the validator service and the clock that drives rounds."""

    def active_window(self, now: datetime) -> RoundWindows | None: ...

    def publication_available_at(self, windows: RoundWindows) -> datetime: ...

    def resolution_due(
        self, round_id: str, horizon_trading_days: int, now: datetime
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class NyseScheduler:
    """Production clock: real sessions, close-relative windows, fetch delay."""

    fetch_delay_seconds: int
    offsets: WindowOffsets = field(default=DEFAULT_OFFSETS)

    def active_window(self, now: datetime) -> RoundWindows | None:
        session = now.astimezone(_NEW_YORK).date()
        if not is_trading_session(session):
            return None
        windows = compute_windows(session, offsets=self.offsets)
        if windows.commit_open <= now <= windows.reveal_close:
            return windows
        return None

    def resolution_due(
        self, round_id: str, horizon_trading_days: int, now: datetime
    ) -> bool:
        t0 = date.fromisoformat(round_id)
        resolution_session = add_trading_days(t0, horizon_trading_days)
        ready_at = session_close(resolution_session).astimezone(UTC) + timedelta(
            seconds=self.fetch_delay_seconds
        )
        return now >= ready_at

    def publication_available_at(self, windows: RoundWindows) -> datetime:
        next_session = add_trading_days(date.fromisoformat(windows.round_id), 1)
        return compute_windows(next_session, offsets=self.offsets).commit_close


@dataclass(frozen=True, slots=True)
class FixedUtcScheduler:
    """24/7 clock for Alpha Risk, anchored at 20:00 UTC every day."""

    fetch_delay_seconds: int
    offsets: WindowOffsets = field(default=DEFAULT_OFFSETS)

    def active_window(self, now: datetime) -> RoundWindows | None:
        now_utc = now.astimezone(UTC)
        windows = compute_fixed_utc_windows(now_utc.date(), offsets=self.offsets)
        if windows.commit_open <= now_utc <= windows.reveal_close:
            return windows
        return None

    def resolution_due(
        self, round_id: str, horizon_trading_days: int, now: datetime
    ) -> bool:
        """Use calendar days; Alpha's stored-window gate does not call this."""
        resolution_day = date.fromisoformat(round_id) + timedelta(
            days=horizon_trading_days
        )
        ready_at = datetime(
            resolution_day.year,
            resolution_day.month,
            resolution_day.day,
            20,
            tzinfo=UTC,
        ) + timedelta(seconds=self.fetch_delay_seconds)
        return now >= ready_at

    def publication_available_at(self, windows: RoundWindows) -> datetime:
        return windows.commit_close + timedelta(days=1)


def scheduler_for_schema(schema_id: str, *, fetch_delay_seconds: int) -> RoundScheduler:
    """Choose the production clock without changing mock/local compression."""
    kind = default_registry().get(schema_id).production_scheduler_kind
    if kind == "fixed_utc":
        return FixedUtcScheduler(fetch_delay_seconds=fetch_delay_seconds)
    return NyseScheduler(fetch_delay_seconds=fetch_delay_seconds)


@dataclass(frozen=True, slots=True)
class SyntheticScheduler:
    """Compressed clock: period ``i`` of wall time is fixture session ``i``."""

    sessions: tuple[date, ...]
    epoch: datetime
    period_seconds: int

    def _period_start(self, index: int) -> datetime:
        return self.epoch + timedelta(seconds=index * self.period_seconds)

    def _windows(self, index: int) -> RoundWindows:
        start = self._period_start(index)
        period = self.period_seconds

        def at(fraction_pct: int) -> datetime:
            return start + timedelta(seconds=period * fraction_pct // 100)

        return RoundWindows(
            round_id=self.sessions[index].isoformat(),
            commit_open=start,
            commit_close=at(40),
            t0_close=at(50),
            reveal_open=at(55),
            reveal_close=at(70),
        )

    def active_window(self, now: datetime) -> RoundWindows | None:
        if now < self.epoch:
            return None
        elapsed = (now - self.epoch).total_seconds()
        index = int(elapsed // self.period_seconds)
        if index >= len(self.sessions):
            return None
        return self._windows(index)

    def publication_available_at(self, windows: RoundWindows) -> datetime:
        return windows.commit_close + timedelta(seconds=self.period_seconds)

    def resolution_due(
        self, round_id: str, horizon_trading_days: int, now: datetime
    ) -> bool:
        try:
            index = self.sessions.index(date.fromisoformat(round_id))
        except ValueError:
            return False
        target = index + horizon_trading_days
        if target >= len(self.sessions):
            return False
        ready_at = self._period_start(target + 1)
        return now >= ready_at
