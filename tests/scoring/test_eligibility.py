"""Deregistration confirmation for scoring-set archival."""

from __future__ import annotations

from endure.protocol.consensus_policy import DEREGISTRATION_CONFIRMATION_SYNCS
from endure.scoring.eligibility import DeregistrationTracker


class TestDeregistrationTracker:
    def test_confirms_after_the_protocol_number_of_missing_generations(self) -> None:
        tracker = DeregistrationTracker()
        tracker.advance({"hk-a", "hk-b"})
        for _ in range(DEREGISTRATION_CONFIRMATION_SYNCS - 1):
            tracker.advance({"hk-a"})
            assert tracker.confirmed() == []

        tracker.advance({"hk-a"})

        assert tracker.confirmed() == ["hk-b"]

    def test_reappearance_resets_the_missing_count(self) -> None:
        tracker = DeregistrationTracker()
        tracker.advance({"hk-a", "hk-b"})
        tracker.advance({"hk-a"})
        tracker.advance({"hk-a", "hk-b"})
        tracker.advance({"hk-a"})

        assert tracker.confirmed() == []

    def test_repeated_reads_between_generations_never_confirm(self) -> None:
        tracker = DeregistrationTracker()
        tracker.advance({"hk-a", "hk-b"})
        tracker.advance({"hk-a"})
        for _ in range(10):
            assert tracker.confirmed() == []

    def test_hotkey_never_seen_registered_is_not_tracked(self) -> None:
        tracker = DeregistrationTracker()
        tracker.advance({"hk-a"})
        tracker.advance({"hk-a"})

        assert tracker.confirmed() == []

    def test_multiple_hotkeys_confirm_sorted(self) -> None:
        tracker = DeregistrationTracker()
        tracker.advance({"hk-c", "hk-a", "hk-b"})
        tracker.advance(set())
        tracker.advance(set())

        assert tracker.confirmed() == ["hk-a", "hk-b", "hk-c"]

    def test_seed_tracks_persisted_hotkeys_absent_at_startup(self) -> None:
        tracker = DeregistrationTracker()
        tracker.seed(registered=["hk-a"], persisted=["hk-gone", "hk-a"])
        tracker.advance({"hk-a"})
        assert tracker.confirmed() == []
        tracker.advance({"hk-a"})

        assert tracker.confirmed() == ["hk-gone"]

    def test_forget_settled_keeps_hotkeys_with_pending_archival(self) -> None:
        tracker = DeregistrationTracker()
        tracker.advance({"hk-archived", "hk-active", "hk-pending"})
        tracker.advance(set())
        tracker.advance(set())

        tracker.forget_settled(
            active_hotkeys={"hk-active"},
            has_unfinished_submission=lambda hotkey: hotkey == "hk-pending",
        )

        assert tracker.confirmed() == ["hk-active", "hk-pending"]
