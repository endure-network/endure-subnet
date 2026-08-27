"""Round windows and the deterministic phase machine.

All boundaries are offsets from the official NYSE close, so half-days shift
automatically and localnet runs compress a round into minutes by swapping the
offsets. The phase function is pure — the validator's orchestration loop and
the axon handlers both derive state from (now, windows) plus the rounds
table, never from in-memory flags.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum

from endure.scoring.oracle.trading_days import is_trading_session, session_close


@dataclass(frozen=True, slots=True)
class WindowOffsets:
    """Close-relative boundaries used by session-based schedulers."""

    commit_open_before_close: timedelta
    commit_close_before_close: timedelta
    reveal_open_after_close: timedelta
    reveal_close_after_close: timedelta

    def __post_init__(self) -> None:
        if self.commit_close_before_close >= self.commit_open_before_close:
            raise ValueError("commit window must open before it closes")
        if self.reveal_close_after_close <= self.reveal_open_after_close:
            raise ValueError("reveal window must open before it closes")


DEFAULT_OFFSETS = WindowOffsets(
    commit_open_before_close=timedelta(hours=9),
    commit_close_before_close=timedelta(minutes=30),
    reveal_open_after_close=timedelta(minutes=30),
    reveal_close_after_close=timedelta(hours=4),
)


@dataclass(frozen=True, slots=True)
class RoundWindows:
    round_id: str
    commit_open: datetime
    commit_close: datetime
    t0_close: datetime
    reveal_open: datetime
    reveal_close: datetime


class RoundPhase(StrEnum):
    PRE_OPEN = "pre_open"
    COMMIT = "commit"
    AWAITING_CLOSE = "awaiting_close"
    REVEAL = "reveal"
    CLOSED = "closed"


def compute_windows(session: date, *, offsets: WindowOffsets) -> RoundWindows:
    """Round boundaries for a trading session, in UTC."""
    if not is_trading_session(session):
        raise ValueError(f"{session} is not an NYSE session")
    close = session_close(session).astimezone(UTC)
    return RoundWindows(
        round_id=session.isoformat(),
        commit_open=close - offsets.commit_open_before_close,
        commit_close=close - offsets.commit_close_before_close,
        t0_close=close,
        reveal_open=close + offsets.reveal_open_after_close,
        reveal_close=close + offsets.reveal_close_after_close,
    )


def compute_fixed_utc_windows(
    anchor_day: date, *, offsets: WindowOffsets
) -> RoundWindows:
    """Calendar-independent round boundaries anchored at 20:00 UTC."""
    close = datetime(anchor_day.year, anchor_day.month, anchor_day.day, 20, tzinfo=UTC)
    return RoundWindows(
        round_id=anchor_day.isoformat(),
        commit_open=close - offsets.commit_open_before_close,
        commit_close=close - offsets.commit_close_before_close,
        t0_close=close,
        reveal_open=close + offsets.reveal_open_after_close,
        reveal_close=close + offsets.reveal_close_after_close,
    )


def phase_at(now: datetime, windows: RoundWindows) -> RoundPhase:
    """Phase of the round at ``now`` — inclusive opens, inclusive closes."""
    if now < windows.commit_open:
        return RoundPhase.PRE_OPEN
    if now <= windows.commit_close:
        return RoundPhase.COMMIT
    if now < windows.reveal_open:
        return RoundPhase.AWAITING_CLOSE
    if now <= windows.reveal_close:
        return RoundPhase.REVEAL
    return RoundPhase.CLOSED
