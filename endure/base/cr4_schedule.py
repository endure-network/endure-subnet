from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Final

_COMMIT_INCLUSION_BLOCK_OFFSET: Final = 1
_MAX_REVEAL_PERIOD_EPOCHS: Final = 100
_MAX_TEMPO_BLOCKS: Final = 50_400


class Cr4ScheduleError(ValueError):
    """Raised when chain values cannot produce a bounded CR4 schedule."""


@dataclass(frozen=True, slots=True)
class Cr4EpochSchedule:
    last_epoch_block: int
    pending_epoch_at: int
    subnet_epoch_index: int
    tempo: int
    blocks_since_last_step: int
    current_block: int


def _runs_epoch(schedule: Cr4EpochSchedule, block: int) -> bool:
    return (
        (schedule.pending_epoch_at > 0 and block >= schedule.pending_epoch_at)
        or schedule.blocks_since_last_step > _MAX_TEMPO_BLOCKS
        or (block - schedule.last_epoch_block >= schedule.tempo)
    )


def _epoch_before_coinbase(schedule: Cr4EpochSchedule, block: int) -> int:
    return schedule.subnet_epoch_index + int(_runs_epoch(schedule, block))


def _advance(schedule: Cr4EpochSchedule, block: int) -> Cr4EpochSchedule:
    elapsed = block - schedule.current_block
    advanced = replace(
        schedule,
        blocks_since_last_step=schedule.blocks_since_last_step + elapsed,
        current_block=block,
    )
    return replace(
        advanced,
        last_epoch_block=block,
        pending_epoch_at=0,
        subnet_epoch_index=advanced.subnet_epoch_index + 1,
        blocks_since_last_step=0,
    )


def _next_epoch_block(schedule: Cr4EpochSchedule) -> int:
    candidates = [
        schedule.last_epoch_block + schedule.tempo,
        schedule.current_block
        + max(1, _MAX_TEMPO_BLOCKS - schedule.blocks_since_last_step + 1),
    ]
    if schedule.pending_epoch_at > 0:
        candidates.append(schedule.pending_epoch_at)
    return max(schedule.current_block + 1, min(candidates))


def _epoch_start_after_commit(
    schedule: Cr4EpochSchedule, epochs_after_commit: int
) -> int:
    if schedule.tempo <= 0 or epochs_after_commit <= 0:
        raise Cr4ScheduleError(
            "CR4 epoch schedule requires positive tempo and reveal period"
        )
    inclusion_block = schedule.current_block + _COMMIT_INCLUSION_BLOCK_OFFSET
    commit_epoch = _epoch_before_coinbase(schedule, inclusion_block)
    target_epoch = commit_epoch + epochs_after_commit
    state = schedule
    for _ in range(epochs_after_commit + 1):
        epoch_block = _next_epoch_block(state)
        if state.subnet_epoch_index + 1 == target_epoch and _runs_epoch(
            state, epoch_block
        ):
            return epoch_block
        state = _advance(state, epoch_block)
        if state.subnet_epoch_index == target_epoch:
            return epoch_block + 1
    raise Cr4ScheduleError("CR4 reveal schedule exceeded its bounded search")


def cr4_reveal_deadline_block(
    schedule: Cr4EpochSchedule,
    *,
    reveal_period_epochs: int,
    finality_margin_blocks: int,
) -> int:
    if finality_margin_blocks < 0:
        raise Cr4ScheduleError("finality margin must be non-negative")
    if reveal_period_epochs > _MAX_REVEAL_PERIOD_EPOCHS:
        raise Cr4ScheduleError("CR4 reveal period exceeds evidence bound")
    expiry_epoch = reveal_period_epochs + 1
    return _epoch_start_after_commit(schedule, expiry_epoch) + finality_margin_blocks
