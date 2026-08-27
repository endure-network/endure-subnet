"""Vertical round behavior (spec §2; Alpha Risk scope spec §Scoring)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, Protocol

import bittensor as bt
from pydantic import ValidationError

from endure.aggregation.assessment_consensus import (
    AssessmentConsensusBundle,
    AssessmentConsensusBundleModel,
    compute_assessment_consensus,
)
from endure.assessment.coordinates import AssessmentConsensusRow
from endure.protocol.round_engine import RoundWindows
from endure.protocol.schedulers import RoundScheduler
from endure.storage.repository import Storage

if TYPE_CHECKING:
    from endure.scoring.assessment_orchestrator import AssessmentScoringOrchestrator


class RoundProgram(Protocol):
    @property
    def horizons(self) -> tuple[int, ...]: ...

    def weights(self) -> dict[str, Decimal]: ...  # D1

    def blended_scores(self) -> dict[str, Decimal]: ...  # D2

    def publish_consensus(self, round_id: str, now: datetime) -> bool: ...  # D3-D5

    def resolve_due(
        self,
        round_id: str,
        windows: RoundWindows,
        now: datetime,
        expected_miners: Sequence[str],
    ) -> tuple[bool, str | None]: ...  # D6


type PublisherProjection = Literal["assessment", "risk"]


@dataclass(frozen=True, slots=True)
class VerticalRuntime:
    round_program: RoundProgram  # D1-D6
    publisher: PublisherProjection  # D8-D9
    scheduler: RoundScheduler  # D11


@dataclass(frozen=True, slots=True)
class AssessmentRoundProgram:
    storage: Storage
    schema_id: str
    bundle_model: AssessmentConsensusBundleModel
    orchestrator: AssessmentScoringOrchestrator
    horizons: tuple[int, ...]
    due_seconds_by_horizon: Mapping[int, int]

    def weights(self) -> dict[str, Decimal]:
        return self.orchestrator.weights()

    def blended_scores(self) -> dict[str, Decimal]:
        return self.orchestrator.blended_scores()

    def publish_consensus(self, round_id: str, now: datetime) -> bool:
        blends = self.orchestrator.blended_scores()
        parseable_hotkeys: set[str] = set()

        def consensus_rows(
            accepted_bundles: list[tuple[str, str]],
        ) -> list[AssessmentConsensusRow]:
            bundles: dict[str, AssessmentConsensusBundle] = {}
            for hotkey, bundle_json in accepted_bundles:
                try:
                    bundles[hotkey] = self.bundle_model.model_validate_json(bundle_json)
                    parseable_hotkeys.add(hotkey)
                except ValidationError as error:
                    # An accepted bundle that no longer parses (a schema tightened
                    # under a later key, or a corrupted row) must not keep the
                    # round from revealing; scoring already skips such a miner,
                    # so consensus applies the same policy.
                    bt.logging.error(
                        f"accepted bundle for {hotkey} in round {round_id} failed "
                        "to parse during consensus publication — skipping miner: "
                        f"{type(error).__name__}"
                    )
            return compute_assessment_consensus(bundles, blends) if bundles else []

        _, rows = (
            self.storage.publish_assessment_consensus_from_accepted_bundles_and_reveal(
                round_id,
                self.schema_id,
                consensus_rows,
                now_iso=now.isoformat(),
            )
        )
        if rows:
            bt.logging.info(
                f"assessment consensus published for round {round_id}: {len(rows)} rows"
            )
        return bool(parseable_hotkeys)

    def resolve_due(
        self,
        round_id: str,
        windows: RoundWindows,
        now: datetime,
        expected_miners: Sequence[str],
    ) -> tuple[bool, str | None]:
        del expected_miners
        all_resolved = True
        scored_this_round = False
        last_error: str | None = None
        for horizon in self.horizons:
            if self.storage.has_assessment_resolution_marker(
                round_id, self.schema_id, horizon
            ):
                continue
            due_seconds = self.due_seconds_by_horizon.get(horizon, horizon)
            due_at = windows.reveal_close + timedelta(seconds=due_seconds)
            if now <= due_at:
                all_resolved = False
                continue
            try:
                self.orchestrator.resolve_and_score(
                    round_id,
                    horizon,
                    now_iso=now.isoformat(),
                    resolution_due_at=due_at,
                    archive_hotkeys=(),
                )
            except Exception as error:  # noqa: BLE001 — copied D6 per-horizon containment
                last_error = type(error).__name__
                all_resolved = False
                bt.logging.error(
                    f"assessment round {round_id} horizon {horizon} "
                    f"resolution failed: {error}"
                )
                continue
            scored_this_round = True
        if all_resolved and all(
            self.storage.has_assessment_resolution_marker(
                round_id, self.schema_id, horizon
            )
            for horizon in self.horizons
        ):
            self.storage.set_round_state(
                round_id, self.schema_id, "closed", now_iso=now.isoformat()
            )
        elif scored_this_round:
            self.storage.set_round_state(
                round_id,
                self.schema_id,
                "partially_scored",
                now_iso=now.isoformat(),
            )
        return scored_this_round, last_error
