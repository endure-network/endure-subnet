"""Validator round orchestration (spec §2, §11).

One ``tick()`` advances every pending state transition derivable from
(now, storage): open the active round with its frozen universe, lift the
embargo when the reveal window closes, resolve and score due horizons, close
finished rounds. All state lives in the database — a restarted validator
resumes exactly where the previous process stopped.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal

import bittensor as bt

from endure.assessment.registry import UniverseProvider, UniverseSnapshot
from endure.protocol.schedulers import RoundScheduler
from endure.protocol.vertical import RoundProgram
from endure.storage.repository import Storage


class ValidatorRoundService:
    def __init__(
        self,
        *,
        storage: Storage,
        scheduler: RoundScheduler,
        universe_provider: UniverseProvider,
        schema_id: str,
        horizons: tuple[int, ...],
        now_fn: Callable[[], datetime],
        round_program: RoundProgram,
        max_universe_targets: int | None = None,
    ) -> None:
        self._storage = storage
        self._scheduler = scheduler
        self._universe_provider = universe_provider
        self._schema_id = schema_id
        self._horizons = horizons
        self._round_program = round_program
        self._now_fn = now_fn
        self._max_universe_targets = max_universe_targets
        # A universe-fetch failure is degraded-but-non-fatal: the tick swallows
        # it so the loop survives, but a swallowed failure must not read as
        # healthy. These count consecutive failures (reset on the next
        # successful open) so /health can surface "no round is opening" — the
        # worst soak failure is the silent one.
        self._universe_failures = 0
        self._last_universe_error: str | None = None
        # Round processing is contained per round (a wedged round must not
        # block newer ones), so an exception no longer fails the whole tick.
        # These restore the lost alert: consecutive ticks in which at least
        # one round failed to resolve, reset once a tick clears with none.
        self._resolution_failures = 0
        self._last_resolution_error: str | None = None
        # A round revealing with zero usable accepted submissions trips no
        # failure counter, yet it is how a wedged miner fleet collects nothing
        # for days; count consecutive empties so /health degrades past a
        # threshold.
        self._empty_scored_rounds = 0
        self._last_empty_round: str | None = None

    @property
    def consecutive_universe_failures(self) -> int:
        return self._universe_failures

    @property
    def last_universe_error(self) -> str | None:
        return self._last_universe_error

    @property
    def consecutive_resolution_failures(self) -> int:
        return self._resolution_failures

    @property
    def last_resolution_error(self) -> str | None:
        return self._last_resolution_error

    @property
    def consecutive_empty_scored_rounds(self) -> int:
        return self._empty_scored_rounds

    @property
    def last_empty_scored_round(self) -> str | None:
        return self._last_empty_round

    def _record_scored_round_submissions(
        self, round_id: str, *, had_submissions: bool
    ) -> None:
        if had_submissions:
            self._empty_scored_rounds = 0
            self._last_empty_round = None
            return
        self._empty_scored_rounds += 1
        self._last_empty_round = round_id
        bt.logging.warning(
            f"round {round_id} revealed with zero usable accepted submissions "
            f"(consecutive empty scored rounds: {self._empty_scored_rounds})"
        )

    def tick(
        self,
        *,
        expected_miners: Sequence[str],
        archive_hotkeys: Sequence[str] = (),
    ) -> dict[str, Decimal] | None:
        """Advance the loop; returns fresh weights when scoring happened."""
        now = self._now_fn()
        self._open_active_round(now)
        scored = self._advance_rounds(now, expected_miners, archive_hotkeys)
        if not scored:
            return None
        return self._round_program.weights()

    def blended_snapshot(self) -> dict[str, Decimal]:
        """Unfiltered blended scores backing the current weights (spec §2)."""
        return self._round_program.blended_scores()

    def _open_active_round(self, now: datetime) -> None:
        window = self._scheduler.active_window(now)
        if window is None:
            return
        if self._storage.round_state(window.round_id, self._schema_id) is not None:
            return
        try:
            universe = self._universe_provider.fetch_universe(window.round_id)
        except Exception as error:  # noqa: BLE001 — degrade, never crash the loop
            self._universe_failures += 1
            self._last_universe_error = type(error).__name__
            bt.logging.error(f"universe fetch failed for {window.round_id}: {error}")
            return
        try:
            self._validate_universe_for_round(universe, window.round_id)
        except ValueError as error:
            self._universe_failures += 1
            self._last_universe_error = type(error).__name__
            bt.logging.error(f"universe rejected for {window.round_id}: {error}")
            return
        self._storage.open_round(
            windows=window,
            schema_id=self._schema_id,
            universe=universe,
            now_iso=now.isoformat(),
            publication_available_at=self._scheduler.publication_available_at(window),
        )
        self._universe_failures = 0
        self._last_universe_error = None
        bt.logging.info(
            f"round {window.round_id} opened with {len(universe.tickers)} targets"
        )

    def _validate_universe_for_round(
        self, universe: UniverseSnapshot, round_id: str
    ) -> None:
        if universe.round_id != round_id:
            raise ValueError(
                f"universe round_id {universe.round_id!r} does not match {round_id!r}"
            )
        if (
            self._max_universe_targets is not None
            and len(universe.tickers) > self._max_universe_targets
        ):
            raise ValueError(
                f"universe target count {len(universe.tickers)} exceeds cap "
                f"{self._max_universe_targets}"
            )

    def _publish_consensus_and_reveal(self, round_id: str, now: datetime) -> None:
        """Compute the consensus and flip to 'revealed' in one transaction.

        Folding both writes together closes the crash window where a round
        could carry persisted consensus while still reading 'open' (the next
        tick would then recompute consensus over the same bundles).
        """
        had_submissions = self._round_program.publish_consensus(round_id, now)
        self._record_scored_round_submissions(round_id, had_submissions=had_submissions)

    def _advance_rounds(
        self,
        now: datetime,
        expected_miners: Sequence[str],
        archive_hotkeys: Sequence[str] = (),
    ) -> bool:
        scored_any = False
        failed = False
        for round_id in self._storage.unfinished_rounds(self._schema_id):
            try:
                scored, resolution_error = self._advance_one_round(
                    round_id,
                    now,
                    expected_miners,
                )
            except Exception as error:  # noqa: BLE001 — contain per round: a wedged round must not block newer ones
                failed = True
                self._last_resolution_error = type(error).__name__
                bt.logging.error(f"round {round_id} processing failed: {error}")
                continue
            scored_any = scored_any or scored
            if resolution_error is not None:
                failed = True
                self._last_resolution_error = resolution_error
        if archive_hotkeys:
            for horizon in self._horizons:
                if not self._storage.all_unfinished_assessment_rounds_have_resolution_marker(
                    self._schema_id, horizon
                ):
                    continue
                try:
                    archived = self._storage.archive_assessment_ema_horizon(
                        self._schema_id, horizon, archive_hotkeys
                    )
                    scored_any = scored_any or archived
                except Exception as error:  # noqa: BLE001 — archival is retryable and must not discard weights
                    failed = True
                    self._last_resolution_error = type(error).__name__
                    bt.logging.error(
                        f"assessment EMA archival failed for horizon {horizon}: {error}"
                    )
        if failed:
            self._resolution_failures += 1
        else:
            self._resolution_failures = 0
            self._last_resolution_error = None
        return scored_any

    def _advance_one_round(
        self,
        round_id: str,
        now: datetime,
        expected_miners: Sequence[str],
    ) -> tuple[bool, str | None]:
        windows = self._storage.round_windows(round_id, self._schema_id)
        if windows is None:
            return False, None
        state = self._storage.round_state(round_id, self._schema_id)
        if state == "open" and now > windows.reveal_close:
            self._publish_consensus_and_reveal(round_id, now)
            state = "revealed"
        if state not in ("revealed", "partially_scored"):
            return False, None
        return self._round_program.resolve_due(round_id, windows, now, expected_miners)
