"""Deregistration confirmation for scoring-set archival.

Which hotkeys are archived out of the scoring set is a consensus rule:
validators that disagree emit different weights from the same submissions.
Historical eligibility (who owes absence observations) lives with its
transactions in the watched repository; this module owns the two-generation
deregistration confirmation (fairness-deltas spec §1 decision 3).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass

from endure.protocol.consensus_policy import DEREGISTRATION_CONFIRMATION_SYNCS


class DeregistrationTracker:
    """Confirm deregistration across consecutive metagraph resync generations.

    Confirmation counts metagraph resync generations, never scoring-pass
    ticks, which can repeat against one stale snapshot. State is in-memory: a
    restart only delays archival by one confirmation cycle while the hotkey
    keeps receiving zero observations.
    """

    def __init__(self) -> None:
        self._missing_counts: dict[str, int] = {}
        self._last_registered: set[str] = set()

    def seed(self, registered: Iterable[str], persisted: Iterable[str]) -> None:
        """Baseline from durable EMA state, not process history.

        A hotkey whose EMA state persisted while it was already absent from the
        first post-restart metagraph must still enter the missing count, or it
        could never reach archival.
        """
        self._missing_counts = {}
        self._last_registered = set(registered) | set(persisted)

    def advance(self, current: Iterable[str]) -> None:
        """Record one metagraph resync generation."""
        registered = set(current)
        for hotkey in (set(self._missing_counts) | self._last_registered) - registered:
            self._missing_counts[hotkey] = self._missing_counts.get(hotkey, 0) + 1
        for hotkey in registered:
            self._missing_counts.pop(hotkey, None)
        self._last_registered = registered

    def confirmed(self) -> list[str]:
        """Hotkeys confirmed deregistered, sorted for deterministic archival."""
        return sorted(
            hotkey
            for hotkey, missed in self._missing_counts.items()
            if missed >= DEREGISTRATION_CONFIRMATION_SYNCS
        )

    def forget_settled(
        self,
        *,
        active_hotkeys: Collection[str],
        has_unfinished_submission: Callable[[str], bool],
    ) -> None:
        """Stop tracking confirmed hotkeys whose archival is complete.

        A confirmed hotkey stays tracked until its EMA state is archived and no
        unfinished round still holds its submission.
        """
        for hotkey in self.confirmed():
            if hotkey not in active_hotkeys and not has_unfinished_submission(hotkey):
                self._missing_counts.pop(hotkey, None)


@dataclass(frozen=True, slots=True)
class ScoringSet:
    """Who owes observations this tick, and whose EMA state is archived."""

    expected_miners: tuple[str, ...]
    archive_hotkeys: tuple[str, ...]


def scoring_set(
    registered_hotkeys: Iterable[str], tracker: DeregistrationTracker
) -> ScoringSet:
    """Every currently registered hotkey is expected; confirmed deregistrations
    are archived. Historical eligibility (no absence before a hotkey's first
    accepted reveal) is applied by the watched repository."""
    return ScoringSet(
        expected_miners=tuple(registered_hotkeys),
        archive_hotkeys=tuple(tracker.confirmed()),
    )
