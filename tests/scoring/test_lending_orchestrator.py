"""Lending CF scoring orchestration (Forge lending activation spec §Batch 7)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from endure.aggregation.assessment_consensus import compute_assessment_consensus
from endure.assessment.coordinates import AssessmentConsensusRow, AssessmentCoordinate
from endure.assessment.lending_universe import StaticLendingUniverseProvider
from endure.assessment.schemas.forge_lending import (
    FORGE_LENDING_SCHEMA_ID,
    LENDING_HORIZON_SECONDS,
    LendingOutput,
    LendingSubmissionBundle,
)
from endure.protocol.canonical import canonical_bundle_bytes
from endure.protocol.lending_miner import baseline_lending_bundle
from endure.protocol.round_engine import DEFAULT_OFFSETS, compute_windows
from endure.scoring.lending.market_data import (
    AlphaPriceSeries,
    AlphaPriceSnapshot,
    FixtureAlphaPriceProvider,
)
from endure.scoring.lending.orchestrator import LendingScoringOrchestrator
from endure.storage.repository import Storage

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC).isoformat()
ROUND = "2026-07-06"


def _series(netuid: int, prices: list[str]) -> AlphaPriceSeries:
    return AlphaPriceSeries(
        source="test_fixture_v1",
        netuid=netuid,
        snapshots=tuple(
            AlphaPriceSnapshot(
                netuid=netuid,
                block=1000 + i * 100,
                price_tao_per_alpha=Decimal(p),
                tao_reserve_rao=1_000 + i,
            )
            for i, p in enumerate(prices)
        ),
    )


def _provider(netuids: tuple[int, ...] = (44, 8)) -> FixtureAlphaPriceProvider:
    # 20% worst drawdown from entry → retained 8000 bps − 500 buffer = 7500.
    return FixtureAlphaPriceProvider(
        {n: _series(n, ["0.10", "0.09", "0.08", "0.095"]) for n in netuids}
    )


def _open_round_with_universe(storage: Storage, netuids: tuple[int, ...]) -> None:
    universe = StaticLendingUniverseProvider(netuids=netuids).fetch_universe(ROUND)
    storage.open_round(
        windows=compute_windows(date(2026, 7, 6), offsets=DEFAULT_OFFSETS),
        schema_id=FORGE_LENDING_SCHEMA_ID,
        universe=universe,
        now_iso=NOW,
    )


def _accept_baseline_bundle(
    storage: Storage, *, hotkey: str, netuids: tuple[int, ...]
) -> None:
    bundle = baseline_lending_bundle(round_id=ROUND, netuids=netuids)
    bundle_json = canonical_bundle_bytes(bundle.to_canonical_payload()).decode("utf-8")
    storage.record_commit(
        ROUND, FORGE_LENDING_SCHEMA_ID, hotkey, "ab" * 32, now_iso=NOW
    )
    storage.record_reveal(
        ROUND,
        FORGE_LENDING_SCHEMA_ID,
        hotkey,
        bundle_json=bundle_json,
        nonce_hex="cd" * 16,
        accepted=True,
        rejection_code=None,
        now_iso=NOW,
    )


def _orchestrator(
    storage: Storage, provider: FixtureAlphaPriceProvider | None = None
) -> LendingScoringOrchestrator:
    return LendingScoringOrchestrator(
        storage=storage,
        price_provider=provider or _provider(),
        half_life_rounds=2,
    )


def _cf_coordinate(netuid: int) -> AssessmentCoordinate:
    return AssessmentCoordinate.subnet_asset(
        netuid=netuid,
        horizon_seconds=LENDING_HORIZON_SECONDS,
        output=LendingOutput.COLLATERAL_FACTOR.value,
    )


class TestResolveAndScore:
    def test_scores_cf_against_realized_target_and_persists_pass(
        self, storage: Storage
    ) -> None:
        _open_round_with_universe(storage, (44,))
        _accept_baseline_bundle(storage, hotkey="hk-a", netuids=(44,))
        orchestrator = _orchestrator(storage)

        round_scores = orchestrator.resolve_and_score(ROUND, now_iso=NOW)

        # Realized target: retained 8000 − buffer 500 = 7500 bps, resolved.
        [target] = storage.assessment_realized_targets_for(
            ROUND, FORGE_LENDING_SCHEMA_ID
        )
        assert target.coordinate == _cf_coordinate(44)
        assert target.value == Decimal(7500)
        assert target.status == "resolved"
        assert target.provider_payload_hash is not None

        # Baseline CF is 5000: deviation −2500 on the lenient side.
        # grace 200, cutoff 1500 * lenient 3 = 4500 → score = 1 − 2300/4300.
        [score] = storage.assessment_output_scores_for_round(
            ROUND, FORGE_LENDING_SCHEMA_ID
        )
        assert score.miner_hotkey == "hk-a"
        expected = Decimal(1) - (Decimal(2500) - Decimal(200)) / (
            Decimal(4500) - Decimal(200)
        )
        assert abs(score.score - expected) < Decimal("1e-20")
        assert round_scores["hk-a"] == score.score

        # EMA seeded from zero prior and history recorded in the same pass.
        [ema] = storage.assessment_ema_states(FORGE_LENDING_SCHEMA_ID)
        assert ema.miner_hotkey == "hk-a"
        assert ema.resolved_rounds == 1
        assert Decimal(0) < ema.ema < score.score
        [history] = storage.assessment_score_history_for_round(
            ROUND, FORGE_LENDING_SCHEMA_ID
        )
        assert history.ema_after == ema.ema

    def test_netuid_without_market_data_is_voided_not_scored(
        self, storage: Storage
    ) -> None:
        _open_round_with_universe(storage, (44, 8))
        _accept_baseline_bundle(storage, hotkey="hk-a", netuids=(44, 8))
        orchestrator = _orchestrator(storage, _provider(netuids=(44,)))

        orchestrator.resolve_and_score(ROUND, now_iso=NOW)

        targets = {
            target.coordinate.target_id: target
            for target in storage.assessment_realized_targets_for(
                ROUND, FORGE_LENDING_SCHEMA_ID
            )
        }
        assert targets["44"].status == "resolved"
        assert targets["8"].status == "voided"
        assert targets["8"].value is None
        # Only the resolved coordinate is scored; the voided one is not the
        # miner's fault and contributes nothing.
        scores = storage.assessment_output_scores_for_round(
            ROUND, FORGE_LENDING_SCHEMA_ID
        )
        assert {score.coordinate.target_id for score in scores} == {"44"}

    def test_missing_asset_scores_zero_for_that_coordinate(
        self, storage: Storage
    ) -> None:
        # Coverage penalty: a miner that skipped a resolved netuid earns 0 on
        # it, so cherry-picking easy assets cannot beat full coverage.
        _open_round_with_universe(storage, (44, 8))
        _accept_baseline_bundle(storage, hotkey="hk-a", netuids=(44,))
        orchestrator = _orchestrator(storage)

        round_scores = orchestrator.resolve_and_score(ROUND, now_iso=NOW)

        scores = {
            score.coordinate.target_id: score.score
            for score in storage.assessment_output_scores_for_round(
                ROUND, FORGE_LENDING_SCHEMA_ID
            )
        }
        assert scores["8"] == Decimal(0)
        assert scores["44"] > Decimal(0)
        assert round_scores["hk-a"] == (scores["44"] + scores["8"]) / 2

    def test_replay_after_completion_returns_empty_and_never_double_applies(
        self, storage: Storage
    ) -> None:
        _open_round_with_universe(storage, (44,))
        _accept_baseline_bundle(storage, hotkey="hk-a", netuids=(44,))
        orchestrator = _orchestrator(storage)
        first = orchestrator.resolve_and_score(ROUND, now_iso=NOW)
        [ema_before] = storage.assessment_ema_states(FORGE_LENDING_SCHEMA_ID)

        replay = orchestrator.resolve_and_score(ROUND, now_iso=NOW)

        assert first and replay == {}
        [ema_after] = storage.assessment_ema_states(FORGE_LENDING_SCHEMA_ID)
        assert ema_after == ema_before

    def test_zero_submission_round_completes_with_marker_only(
        self, storage: Storage
    ) -> None:
        _open_round_with_universe(storage, (44,))
        orchestrator = _orchestrator(storage)

        round_scores = orchestrator.resolve_and_score(ROUND, now_iso=NOW)

        assert round_scores == {}
        assert storage.assessment_realized_targets_for(ROUND, FORGE_LENDING_SCHEMA_ID)
        assert (
            storage.assessment_output_score_count(ROUND, FORGE_LENDING_SCHEMA_ID) == 0
        )

    def test_assessment_scoring_excludes_reveal_persisted_after_consensus(
        self, storage: Storage
    ) -> None:
        _open_round_with_universe(storage, (44,))
        _accept_baseline_bundle(storage, hotkey="hk-early", netuids=(44,))

        def consensus_rows(
            accepted_bundles: list[tuple[str, str]],
        ) -> list[AssessmentConsensusRow]:
            bundles = {
                hotkey: LendingSubmissionBundle.model_validate_json(bundle_json)
                for hotkey, bundle_json in accepted_bundles
            }
            return compute_assessment_consensus(bundles, {})

        storage.publish_assessment_consensus_from_accepted_bundles_and_reveal(
            ROUND,
            FORGE_LENDING_SCHEMA_ID,
            consensus_rows,
            now_iso=NOW,
        )
        _accept_baseline_bundle(storage, hotkey="hk-late", netuids=(44,))

        consensus = storage.assessment_consensus_for(ROUND, FORGE_LENDING_SCHEMA_ID)
        assert consensus
        assert all(row.n_submitters == 1 for row in consensus)
        assert [
            bundle.miner_hotkey
            for bundle in storage.accepted_assessment_bundles(
                ROUND, FORGE_LENDING_SCHEMA_ID
            )
        ] == ["hk-early"]

        round_scores = _orchestrator(storage).resolve_and_score(ROUND, now_iso=NOW)

        assert set(round_scores) == {"hk-early"}
        assert {
            score.miner_hotkey
            for score in storage.assessment_output_scores_for_round(
                ROUND, FORGE_LENDING_SCHEMA_ID
            )
        } == {"hk-early"}


class TestBlendedScoresAndWeights:
    def test_blended_scores_average_cf_coordinate_emas_per_hotkey(
        self, storage: Storage
    ) -> None:
        _open_round_with_universe(storage, (44, 8))
        _accept_baseline_bundle(storage, hotkey="hk-a", netuids=(44, 8))
        orchestrator = _orchestrator(storage)
        orchestrator.resolve_and_score(ROUND, now_iso=NOW)

        blended = orchestrator.blended_scores()

        emas = storage.assessment_ema_states(FORGE_LENDING_SCHEMA_ID)
        expected = sum((state.ema for state in emas), Decimal(0)) / len(emas)
        assert blended == {"hk-a": expected}

    def test_weights_abstain_as_all_zero_when_no_positive_ema(
        self, storage: Storage
    ) -> None:
        orchestrator = _orchestrator(storage)

        assert orchestrator.blended_scores() == {}
        assert orchestrator.weights() == {}
