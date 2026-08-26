"""Generic assessment-coordinate persistence for Forge lending spec §Batch 2A."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from endure.assessment.coordinates import (
    AssessmentConsensusRow,
    AssessmentCoordinate,
    AssessmentEmaState,
    AssessmentOutputScore,
    AssessmentRealizedTarget,
    AssessmentScoreHistoryRow,
)
from endure.assessment.schemas.forge_lending import (
    FORGE_LENDING_SCHEMA_ID,
    LENDING_HORIZON_SECONDS,
    LendingOutput,
)
from endure.protocol.round_engine import DEFAULT_OFFSETS, compute_windows
from endure.storage.repository import Storage

NOW = datetime(2026, 6, 25, 12, 0, tzinfo=UTC).isoformat()
ROUND = "2026-06-25"


def _open_lending_round(storage: Storage) -> None:
    storage.open_round(
        windows=compute_windows(date(2026, 6, 25), offsets=DEFAULT_OFFSETS),
        schema_id=FORGE_LENDING_SCHEMA_ID,
        universe=None,
        now_iso=NOW,
    )


def _coordinate(output: LendingOutput) -> AssessmentCoordinate:
    return AssessmentCoordinate.subnet_asset(
        netuid=42,
        horizon_seconds=LENDING_HORIZON_SECONDS,
        output=output.value,
    )


def test_existing_submission_audit_trail_holds_accepted_lending_bundles(
    storage: Storage,
) -> None:
    _open_lending_round(storage)
    storage.record_commit(
        ROUND, FORGE_LENDING_SCHEMA_ID, "hk-a", "ab" * 32, now_iso=NOW
    )

    storage.record_reveal(
        ROUND,
        FORGE_LENDING_SCHEMA_ID,
        "hk-a",
        bundle_json='{"schema_id":"lending.v1.subnet_asset","assets":[]}',
        nonce_hex="01",
        accepted=True,
        rejection_code=None,
        now_iso=NOW,
    )

    bundles = storage.accepted_assessment_bundles(ROUND, FORGE_LENDING_SCHEMA_ID)
    assert [bundle.miner_hotkey for bundle in bundles] == ["hk-a"]
    assert bundles[0].bundle_json.startswith('{"schema_id":"lending')


def test_unfinished_assessment_submission_requires_accepted_open_round(
    storage: Storage,
) -> None:
    _open_lending_round(storage)

    assert (
        storage.has_unfinished_assessment_submission(
            FORGE_LENDING_SCHEMA_ID, "hk-missing"
        )
        is False
    )

    storage.record_commit(
        ROUND, FORGE_LENDING_SCHEMA_ID, "hk-rejected", "cd" * 32, now_iso=NOW
    )
    storage.record_reveal(
        ROUND,
        FORGE_LENDING_SCHEMA_ID,
        "hk-rejected",
        bundle_json='{"schema_id":"lending.v1.subnet_asset","assets":[]}',
        nonce_hex="02",
        accepted=False,
        rejection_code="invalid_bundle",
        now_iso=NOW,
    )
    assert (
        storage.has_unfinished_assessment_submission(
            FORGE_LENDING_SCHEMA_ID, "hk-rejected"
        )
        is False
    )

    storage.record_commit(
        ROUND, FORGE_LENDING_SCHEMA_ID, "hk-accepted", "ef" * 32, now_iso=NOW
    )
    storage.record_reveal(
        ROUND,
        FORGE_LENDING_SCHEMA_ID,
        "hk-accepted",
        bundle_json='{"schema_id":"lending.v1.subnet_asset","assets":[]}',
        nonce_hex="03",
        accepted=True,
        rejection_code=None,
        now_iso=NOW,
    )
    assert (
        storage.has_unfinished_assessment_submission(
            FORGE_LENDING_SCHEMA_ID, "hk-accepted"
        )
        is True
    )

    storage.set_round_state(ROUND, FORGE_LENDING_SCHEMA_ID, "closed", now_iso=NOW)

    assert (
        storage.has_unfinished_assessment_submission(
            FORGE_LENDING_SCHEMA_ID, "hk-accepted"
        )
        is False
    )


def test_generic_storage_round_trips_one_lending_round_for_all_outputs(
    storage: Storage,
) -> None:
    _open_lending_round(storage)
    consensus_rows = [
        AssessmentConsensusRow(
            coordinate=_coordinate(output),
            value=Decimal(index + 1),
            dispersion=Decimal("0.01"),
            n_submitters=3,
        )
        for index, output in enumerate(LendingOutput)
    ]
    output_scores = [
        AssessmentOutputScore(
            miner_hotkey="hk-a",
            coordinate=_coordinate(output),
            score=Decimal("0.9"),
            error=Decimal("0.1"),
        )
        for output in LendingOutput
    ]
    ema_states = [
        AssessmentEmaState(
            miner_hotkey="hk-a",
            coordinate=_coordinate(output),
            ema=Decimal("0.8"),
            resolved_rounds=2,
        )
        for output in LendingOutput
    ]
    history_rows = [
        AssessmentScoreHistoryRow(
            miner_hotkey="hk-a",
            coordinate=_coordinate(output),
            round_score=Decimal("0.9"),
            ema_after=Decimal("0.8"),
        )
        for output in LendingOutput
    ]

    storage.publish_assessment_consensus_and_reveal(
        ROUND, FORGE_LENDING_SCHEMA_ID, consensus_rows, now_iso=NOW
    )
    storage.record_assessment_output_scores(
        ROUND, FORGE_LENDING_SCHEMA_ID, output_scores, now_iso=NOW
    )
    for state in ema_states:
        storage.upsert_assessment_ema(
            FORGE_LENDING_SCHEMA_ID,
            state,
            now_iso=NOW,
        )
    storage.record_assessment_score_history(
        ROUND, FORGE_LENDING_SCHEMA_ID, history_rows, now_iso=NOW
    )

    loaded_consensus = storage.assessment_consensus_for(ROUND, FORGE_LENDING_SCHEMA_ID)
    loaded_scores = storage.assessment_output_scores_for_round(
        ROUND, FORGE_LENDING_SCHEMA_ID
    )
    loaded_emas = storage.assessment_ema_states(FORGE_LENDING_SCHEMA_ID)
    loaded_history = storage.assessment_score_history_for_round(
        ROUND, FORGE_LENDING_SCHEMA_ID
    )

    assert {row.coordinate.output for row in loaded_consensus} == {
        output.value for output in LendingOutput
    }
    assert storage.assessment_output_score_count(ROUND, FORGE_LENDING_SCHEMA_ID) == 7
    assert {row.coordinate.output for row in loaded_scores} == {
        output.value for output in LendingOutput
    }
    assert {row.coordinate.output for row in loaded_emas} == {
        output.value for output in LendingOutput
    }
    assert {row.coordinate.output for row in loaded_history} == {
        output.value for output in LendingOutput
    }
    assert {
        (
            row.coordinate.target_kind,
            row.coordinate.target_id,
            row.coordinate.horizon_kind,
            row.coordinate.horizon_value,
        )
        for row in loaded_consensus
    } == {("subnet_asset", "42", "seconds", LENDING_HORIZON_SECONDS)}


def test_generic_storage_records_realized_collateral_factor_target(
    storage: Storage,
) -> None:
    _open_lending_round(storage)
    realized = AssessmentRealizedTarget(
        coordinate=_coordinate(LendingOutput.COLLATERAL_FACTOR),
        value=Decimal("6500"),
        status="resolved",
        provider_payload_hash="payload-hash",
    )

    storage.record_assessment_realized_targets(
        ROUND, FORGE_LENDING_SCHEMA_ID, [realized], now_iso=NOW
    )
    storage.record_assessment_realized_targets(
        ROUND,
        FORGE_LENDING_SCHEMA_ID,
        [
            AssessmentRealizedTarget(
                coordinate=_coordinate(LendingOutput.COLLATERAL_FACTOR),
                value=Decimal("1"),
                status="resolved",
                provider_payload_hash="later",
            )
        ],
        now_iso=NOW,
    )

    loaded = storage.assessment_realized_targets_for(ROUND, FORGE_LENDING_SCHEMA_ID)
    assert loaded == [realized]


def test_generic_ema_upsert_updates_existing_coordinate_state(
    storage: Storage,
) -> None:
    coordinate = _coordinate(LendingOutput.COLLATERAL_FACTOR)

    storage.upsert_assessment_ema(
        FORGE_LENDING_SCHEMA_ID,
        AssessmentEmaState(
            miner_hotkey="hk-a",
            coordinate=coordinate,
            ema=Decimal("0.4"),
            resolved_rounds=1,
        ),
        now_iso=NOW,
    )
    storage.upsert_assessment_ema(
        FORGE_LENDING_SCHEMA_ID,
        AssessmentEmaState(
            miner_hotkey="hk-a",
            coordinate=coordinate,
            ema=Decimal("0.7"),
            resolved_rounds=2,
        ),
        now_iso=NOW,
    )

    loaded = storage.assessment_ema_states(FORGE_LENDING_SCHEMA_ID)
    assert loaded == [
        AssessmentEmaState(
            miner_hotkey="hk-a",
            coordinate=coordinate,
            ema=Decimal("0.7"),
            resolved_rounds=2,
        )
    ]


def test_publish_assessment_consensus_and_reveal_is_atomic(
    storage: Storage,
) -> None:
    _open_lending_round(storage)
    rows = [
        AssessmentConsensusRow(
            coordinate=_coordinate(output),
            value=Decimal(5000),
            dispersion=Decimal(0),
            n_submitters=2,
        )
        for output in LendingOutput
    ]

    storage.publish_assessment_consensus_and_reveal(
        ROUND, FORGE_LENDING_SCHEMA_ID, rows, now_iso=NOW
    )

    loaded = storage.assessment_consensus_for(ROUND, FORGE_LENDING_SCHEMA_ID)
    assert len(loaded) == len(LendingOutput)
    assert storage.round_state(ROUND, FORGE_LENDING_SCHEMA_ID) == "revealed"


def test_publish_assessment_consensus_reveals_zero_submission_round(
    storage: Storage,
) -> None:
    _open_lending_round(storage)

    storage.publish_assessment_consensus_and_reveal(
        ROUND, FORGE_LENDING_SCHEMA_ID, [], now_iso=NOW
    )

    assert storage.assessment_consensus_for(ROUND, FORGE_LENDING_SCHEMA_ID) == []
    assert storage.round_state(ROUND, FORGE_LENDING_SCHEMA_ID) == "revealed"


def test_publish_assessment_consensus_never_overwrites_on_recompute(
    storage: Storage,
) -> None:
    _open_lending_round(storage)
    first = [
        AssessmentConsensusRow(
            coordinate=_coordinate(LendingOutput.COLLATERAL_FACTOR),
            value=Decimal(5000),
            dispersion=Decimal(0),
            n_submitters=1,
        )
    ]
    storage.publish_assessment_consensus_and_reveal(
        ROUND, FORGE_LENDING_SCHEMA_ID, first, now_iso=NOW
    )
    second = [
        AssessmentConsensusRow(
            coordinate=_coordinate(LendingOutput.COLLATERAL_FACTOR),
            value=Decimal(9999),
            dispersion=Decimal(0),
            n_submitters=1,
        )
    ]

    storage.publish_assessment_consensus_and_reveal(
        ROUND, FORGE_LENDING_SCHEMA_ID, second, now_iso=NOW
    )

    loaded = storage.assessment_consensus_for(ROUND, FORGE_LENDING_SCHEMA_ID)
    assert [row.value for row in loaded] == [Decimal(5000)]


def test_publish_assessment_consensus_never_rewinds_a_scored_round(
    storage: Storage,
) -> None:
    _open_lending_round(storage)
    storage.publish_assessment_consensus_and_reveal(
        ROUND, FORGE_LENDING_SCHEMA_ID, [], now_iso=NOW
    )
    storage.set_round_state(ROUND, FORGE_LENDING_SCHEMA_ID, "closed", now_iso=NOW)

    storage.publish_assessment_consensus_and_reveal(
        ROUND, FORGE_LENDING_SCHEMA_ID, [], now_iso=NOW
    )

    assert storage.round_state(ROUND, FORGE_LENDING_SCHEMA_ID) == "closed"


def _scoring_pass_rows() -> tuple[list, list, list, list]:
    coordinate = _coordinate(LendingOutput.COLLATERAL_FACTOR)
    realized = [
        AssessmentRealizedTarget(
            coordinate=coordinate,
            value=Decimal(4800),
            status="resolved",
            provider_payload_hash="ab" * 32,
        )
    ]
    scores = [
        AssessmentOutputScore(
            miner_hotkey="hk-a", coordinate=coordinate, score=Decimal("0.9")
        )
    ]
    emas = [
        AssessmentEmaState(
            miner_hotkey="hk-a",
            coordinate=coordinate,
            ema=Decimal("0.45"),
            resolved_rounds=1,
        )
    ]
    history = [
        AssessmentScoreHistoryRow(
            miner_hotkey="hk-a",
            coordinate=coordinate,
            round_score=Decimal("0.9"),
            ema_after=Decimal("0.45"),
        )
    ]
    return realized, scores, emas, history


class TestRecordAssessmentScoringPass:
    def test_incomplete_pass_persists_progress_without_completion_marker(
        self, storage: Storage
    ) -> None:
        _open_lending_round(storage)
        realized, scores, emas, history = _scoring_pass_rows()

        storage.record_assessment_scoring_pass(
            ROUND,
            FORGE_LENDING_SCHEMA_ID,
            horizon_value=LENDING_HORIZON_SECONDS,
            realized_targets=realized,
            output_scores=scores,
            ema_updates=emas,
            score_history=history,
            complete=False,
            now_iso=NOW,
        )

        assert storage.assessment_realized_targets_for(ROUND, FORGE_LENDING_SCHEMA_ID)
        assert not storage.has_assessment_resolution_marker(
            ROUND, FORGE_LENDING_SCHEMA_ID, LENDING_HORIZON_SECONDS
        )

    def test_writes_all_four_surfaces_in_one_call(self, storage: Storage) -> None:
        _open_lending_round(storage)
        realized, scores, emas, history = _scoring_pass_rows()

        storage.record_assessment_scoring_pass(
            ROUND,
            FORGE_LENDING_SCHEMA_ID,
            horizon_value=LENDING_HORIZON_SECONDS,
            realized_targets=realized,
            output_scores=scores,
            ema_updates=emas,
            score_history=history,
            now_iso=NOW,
        )

        assert storage.assessment_realized_targets_for(ROUND, FORGE_LENDING_SCHEMA_ID)
        assert (
            storage.assessment_output_score_count(ROUND, FORGE_LENDING_SCHEMA_ID) == 1
        )
        [ema] = storage.assessment_ema_states(FORGE_LENDING_SCHEMA_ID)
        assert ema.ema == Decimal("0.45")
        assert ema.resolved_rounds == 1

    def test_replay_of_a_scored_round_is_a_structural_no_op(
        self, storage: Storage
    ) -> None:
        # H-01: EMA idempotency must be structural, not an upstream courtesy.
        # Re-invoking a completed pass (crash-recovery replay, double tick)
        # must not double-apply the EMA or double-count resolved_rounds.
        _open_lending_round(storage)
        realized, scores, emas, history = _scoring_pass_rows()
        storage.record_assessment_scoring_pass(
            ROUND,
            FORGE_LENDING_SCHEMA_ID,
            horizon_value=LENDING_HORIZON_SECONDS,
            realized_targets=realized,
            output_scores=scores,
            ema_updates=emas,
            score_history=history,
            now_iso=NOW,
        )

        drifted = [
            AssessmentEmaState(
                miner_hotkey="hk-a",
                coordinate=_coordinate(LendingOutput.COLLATERAL_FACTOR),
                ema=Decimal("0.99"),
                resolved_rounds=2,
            )
        ]
        storage.record_assessment_scoring_pass(
            ROUND,
            FORGE_LENDING_SCHEMA_ID,
            horizon_value=LENDING_HORIZON_SECONDS,
            realized_targets=realized,
            output_scores=scores,
            ema_updates=drifted,
            score_history=history,
            now_iso=NOW,
        )

        [ema] = storage.assessment_ema_states(FORGE_LENDING_SCHEMA_ID)
        assert ema.ema == Decimal("0.45")
        assert ema.resolved_rounds == 1
        assert (
            storage.assessment_output_score_count(ROUND, FORGE_LENDING_SCHEMA_ID) == 1
        )

    def test_zero_submission_round_still_records_completion_marker(
        self, storage: Storage
    ) -> None:
        _open_lending_round(storage)
        realized, _, _, _ = _scoring_pass_rows()

        storage.record_assessment_scoring_pass(
            ROUND,
            FORGE_LENDING_SCHEMA_ID,
            horizon_value=LENDING_HORIZON_SECONDS,
            realized_targets=realized,
            output_scores=[],
            ema_updates=[],
            score_history=[],
            now_iso=NOW,
        )

        assert storage.assessment_realized_targets_for(ROUND, FORGE_LENDING_SCHEMA_ID)
        assert storage.has_assessment_resolution_marker(
            ROUND, FORGE_LENDING_SCHEMA_ID, LENDING_HORIZON_SECONDS
        )
        assert (
            storage.assessment_output_score_count(ROUND, FORGE_LENDING_SCHEMA_ID) == 0
        )

    def test_empty_target_pass_still_records_completion_marker(
        self, storage: Storage
    ) -> None:
        _open_lending_round(storage)

        storage.record_assessment_scoring_pass(
            ROUND,
            FORGE_LENDING_SCHEMA_ID,
            horizon_value=LENDING_HORIZON_SECONDS,
            realized_targets=[],
            output_scores=[],
            ema_updates=[],
            score_history=[],
            now_iso=NOW,
        )

        assert storage.has_assessment_resolution_marker(
            ROUND, FORGE_LENDING_SCHEMA_ID, LENDING_HORIZON_SECONDS
        )
        assert not storage.assessment_realized_targets_for(
            ROUND, FORGE_LENDING_SCHEMA_ID
        )
