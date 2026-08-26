"""Validator FastAPI read surface with an Alpha Risk V1 embargo gate."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import TypedDict

from endure.storage.repository import AssessmentRoundResolutionProgress


class HorizonResolutionHealth(TypedDict):
    horizon_seconds: int
    due_at: str


class RoundResolutionHealthDetail(TypedDict):
    round_id: str
    pending_horizons: list[HorizonResolutionHealth]
    overdue_horizons: list[HorizonResolutionHealth]


class RoundResolutionHealth(TypedDict):
    pending_round_count: int
    overdue_round_count: int
    pending_rounds: list[RoundResolutionHealthDetail]
    overdue_rounds: list[RoundResolutionHealthDetail]


def assessment_round_resolution_health(
    progress_rows: Sequence[AssessmentRoundResolutionProgress],
    horizons: Sequence[int],
    *,
    now: datetime,
    sample_limit: int,
    due_seconds: Mapping[int, int] | None = None,
) -> RoundResolutionHealth:
    """Classify unfinished rounds as expected pending or operationally overdue."""
    pending_rounds: list[RoundResolutionHealthDetail] = []
    overdue_rounds: list[RoundResolutionHealthDetail] = []
    pending_count = 0
    overdue_count = 0
    for progress in progress_rows:
        pending_horizons: list[HorizonResolutionHealth] = []
        overdue_horizons: list[HorizonResolutionHealth] = []
        for horizon in horizons:
            if horizon in progress.resolved_horizons:
                continue
            effective_due_seconds = (
                horizon if due_seconds is None else due_seconds.get(horizon, horizon)
            )
            due_at = progress.reveal_close_at + timedelta(seconds=effective_due_seconds)
            horizon_health: HorizonResolutionHealth = {
                "horizon_seconds": horizon,
                "due_at": due_at.isoformat(),
            }
            if now > due_at:
                overdue_horizons.append(horizon_health)
            else:
                pending_horizons.append(horizon_health)
        detail: RoundResolutionHealthDetail = {
            "round_id": progress.round_id,
            "pending_horizons": pending_horizons,
            "overdue_horizons": overdue_horizons,
        }
        if overdue_horizons:
            overdue_count += 1
            if len(overdue_rounds) < sample_limit:
                overdue_rounds.append(detail)
        elif pending_horizons:
            pending_count += 1
            if len(pending_rounds) < sample_limit:
                pending_rounds.append(detail)
    return {
        "pending_round_count": pending_count,
        "overdue_round_count": overdue_count,
        "pending_rounds": pending_rounds,
        "overdue_rounds": overdue_rounds,
    }
