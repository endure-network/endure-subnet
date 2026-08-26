import pytest

from endure.base import cr4_schedule
from endure.base.cr4_schedule import Cr4EpochSchedule, cr4_reveal_deadline_block


def test_tempo_360_reveal_period_one_expires_after_reveal_epoch() -> None:
    schedule = Cr4EpochSchedule(
        last_epoch_block=0,
        pending_epoch_at=0,
        subnet_epoch_index=0,
        tempo=360,
        blocks_since_last_step=7,
        current_block=7,
    )

    deadline = cr4_reveal_deadline_block(
        schedule,
        reveal_period_epochs=1,
        finality_margin_blocks=12,
    )

    assert deadline == 732


def test_commit_on_epoch_boundary_uses_the_new_commit_epoch() -> None:
    schedule = Cr4EpochSchedule(
        last_epoch_block=0,
        pending_epoch_at=0,
        subnet_epoch_index=0,
        tempo=360,
        blocks_since_last_step=359,
        current_block=359,
    )

    deadline = cr4_reveal_deadline_block(
        schedule,
        reveal_period_epochs=1,
        finality_margin_blocks=12,
    )

    assert deadline == 1092


def test_reveal_period_above_evidence_bound_is_rejected() -> None:
    schedule = Cr4EpochSchedule(
        last_epoch_block=0,
        pending_epoch_at=0,
        subnet_epoch_index=0,
        tempo=1,
        blocks_since_last_step=0,
        current_block=0,
    )

    with pytest.raises(ValueError, match="reveal period exceeds evidence bound"):
        cr4_reveal_deadline_block(
            schedule,
            reveal_period_epochs=101,
            finality_margin_blocks=12,
        )


def test_schedule_advances_once_per_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schedule = Cr4EpochSchedule(
        last_epoch_block=0,
        pending_epoch_at=0,
        subnet_epoch_index=0,
        tempo=360,
        blocks_since_last_step=0,
        current_block=0,
    )
    advance = cr4_schedule._advance
    calls = 0

    def counted_advance(state: Cr4EpochSchedule, block: int) -> Cr4EpochSchedule:
        nonlocal calls
        calls += 1
        return advance(state, block)

    monkeypatch.setattr(cr4_schedule, "_advance", counted_advance)

    deadline = cr4_reveal_deadline_block(
        schedule,
        reveal_period_epochs=10,
        finality_margin_blocks=12,
    )

    assert deadline == 3972
    assert calls <= 11


def test_pending_owner_epoch_preempts_tempo() -> None:
    deadline = cr4_reveal_deadline_block(
        Cr4EpochSchedule(
            last_epoch_block=0,
            pending_epoch_at=100,
            subnet_epoch_index=0,
            tempo=360,
            blocks_since_last_step=7,
            current_block=7,
        ),
        reveal_period_epochs=1,
        finality_margin_blocks=12,
    )

    assert deadline == 472


def test_forced_epoch_preserves_pre_coinbase_visibility() -> None:
    deadline = cr4_reveal_deadline_block(
        Cr4EpochSchedule(
            last_epoch_block=100,
            pending_epoch_at=0,
            subnet_epoch_index=0,
            tempo=50_400,
            blocks_since_last_step=50_400,
            current_block=100,
        ),
        reveal_period_epochs=1,
        finality_margin_blocks=12,
    )

    assert deadline == 50_513
