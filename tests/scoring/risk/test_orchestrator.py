from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

import endure.scoring.risk.orchestrator as risk_orchestrator_module
from endure.assessment.coordinates import AssessmentCoordinate, AssessmentEmaState
from endure.assessment.schemas.subnet_alpha_risk import (
    HORIZON_5D_SECONDS,
    RISK_HORIZONS,
    RISK_SCHEMA_ID,
    RiskOutput,
)
from endure.assessment.subnet_alpha_universe import StaticAlphaRiskUniverseProvider
from endure.protocol.canonical import canonical_bundle_bytes
from endure.protocol.risk_miner import LatestPoolObservation, baseline_risk_bundle
from endure.protocol.round_engine import DEFAULT_OFFSETS, compute_windows
from endure.scoring.market_data import (
    AlphaMarketDataError,
    AlphaMarketDataUnavailable,
    AlphaPriceProvider,
    AlphaPriceSeries,
    AlphaPriceSnapshot,
    FixtureAlphaPriceProvider,
    ResolutionWindow,
    recorded_mainnet_fixture_provider,
)
from endure.scoring.risk.observables import (
    filter_snapshots_for_window,
    horizon_seconds_to_blocks,
)
from endure.scoring.risk.orchestrator import (
    VOID_GRACE_SECONDS,
    RiskScoringOrchestrator,
)
from endure.storage.repository import Storage

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC).isoformat()
ROUND = "2026-07-06"
WINDOW_START_BLOCK = 1_000
RECORDED_FIXTURE_ANCHOR_BLOCK = 7_402_800


class _FailingPriceProvider:
    def __init__(
        self, delegate: FixtureAlphaPriceProvider, *, failures_remaining: int | None
    ) -> None:
        self._delegate = delegate
        self._failures_remaining = failures_remaining

    def price_series(
        self, netuid: int, *, window: ResolutionWindow
    ) -> AlphaPriceSeries | None:
        failures_remaining = self._failures_remaining
        if failures_remaining is None:
            raise AlphaMarketDataError("archive unavailable")
        if failures_remaining > 0:
            self._failures_remaining = failures_remaining - 1
            raise AlphaMarketDataError("archive unavailable")
        return self._delegate.price_series(netuid, window=window)


def _unavailable_reveal_close_block(_reveal_close: datetime) -> int:
    raise AlphaMarketDataUnavailable("archive unavailable")


def _missing_archive_timestamp(_timestamp: datetime) -> int:
    raise LookupError("archive missing Timestamp.Now")


def _coordinate(netuid: int, output: RiskOutput, horizon: int) -> AssessmentCoordinate:
    return AssessmentCoordinate.subnet_asset(
        netuid=netuid, horizon_seconds=horizon, output=output.value
    )


def _snapshot(
    block: int, price: str, reserve: int, *, netuid: int
) -> AlphaPriceSnapshot:
    return AlphaPriceSnapshot(
        netuid=netuid,
        block=block,
        price_tao_per_alpha=Decimal(price),
        tao_reserve_rao=reserve,
    )


def _risk_fixture_series(netuid: int) -> AlphaPriceSeries:
    snapshots = tuple(
        _snapshot(
            WINDOW_START_BLOCK + index * 600,
            str(Decimal("1.00") - Decimal(index) / Decimal(10_000)),
            1_000_000_000 + index * 100,
            netuid=netuid,
        )
        for index in range(1, 362)
    )
    return AlphaPriceSeries(
        source=f"risk_fixture_{netuid}",
        netuid=netuid,
        snapshots=snapshots,
    )


def _provider(netuids: tuple[int, ...]) -> FixtureAlphaPriceProvider:
    return FixtureAlphaPriceProvider(
        series_by_netuid={netuid: _risk_fixture_series(netuid) for netuid in netuids}
    )


def _open_round(storage: Storage, netuids: tuple[int, ...]) -> None:
    storage.open_round(
        windows=compute_windows(date(2026, 7, 6), offsets=DEFAULT_OFFSETS),
        schema_id=RISK_SCHEMA_ID,
        universe=StaticAlphaRiskUniverseProvider(netuids=netuids).fetch_universe(ROUND),
        now_iso=NOW,
    )


def _latest_for(
    provider: FixtureAlphaPriceProvider,
) -> Callable[[int], LatestPoolObservation | None]:
    def latest(netuid: int) -> LatestPoolObservation | None:
        return provider.latest_pool_observation(netuid)

    return latest


def _accept_risk_bundle(
    storage: Storage,
    *,
    hotkey: str,
    netuids: tuple[int, ...],
    provider: FixtureAlphaPriceProvider,
) -> None:
    bundle = baseline_risk_bundle(
        round_id=ROUND,
        netuids=netuids,
        latest_observation=_latest_for(provider),
    )
    bundle_json = canonical_bundle_bytes(bundle.to_canonical_payload()).decode("utf-8")
    storage.record_commit(ROUND, RISK_SCHEMA_ID, hotkey, "ab" * 32, now_iso=NOW)
    storage.record_reveal(
        ROUND,
        RISK_SCHEMA_ID,
        hotkey,
        bundle_json=bundle_json,
        nonce_hex="cd" * 16,
        accepted=True,
        rejection_code=None,
        now_iso=NOW,
    )


def _orchestrator(
    storage: Storage, provider: AlphaPriceProvider
) -> RiskScoringOrchestrator:
    return RiskScoringOrchestrator(
        storage=storage,
        price_provider=provider,
        half_life_rounds=2,
        reveal_close_block=lambda reveal_close: WINDOW_START_BLOCK,
    )


class TestForceVoidOrder:
    def test_force_void_order_skips_window_and_market_data_lookups(
        self, storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Given: the block lookup remains unavailable beyond its void deadline.
        provider = MagicMock(spec=AlphaPriceProvider)
        _open_round(storage, (44,))
        windows = storage.round_windows(ROUND, RISK_SCHEMA_ID)
        assert windows is not None
        resolution_due_at = windows.reveal_close + timedelta(seconds=1)
        after_grace = resolution_due_at + timedelta(seconds=VOID_GRACE_SECONDS + 1)
        orchestrator = RiskScoringOrchestrator(
            storage=storage,
            price_provider=provider,
            half_life_rounds=2,
            reveal_close_block=_unavailable_reveal_close_block,
        )
        window_start_block = MagicMock()
        window_end_block = MagicMock()

        class ForceVoidContextProbe:
            force_void_unavailable_targets = True
            void_unavailable_targets = True

            @property
            def window_start_block(self) -> int | None:
                return window_start_block()

            @property
            def window_end_block(self) -> int | None:
                return window_end_block()

        context_factory = MagicMock(return_value=ForceVoidContextProbe())
        monkeypatch.setattr(
            risk_orchestrator_module, "AssessmentResolutionContext", context_factory
        )

        # When: resolution force-voids every coordinate after the grace deadline.
        result = orchestrator.resolve_and_score(
            ROUND,
            HORIZON_5D_SECONDS,
            now_iso=after_grace.isoformat(),
            resolution_due_at=resolution_due_at,
        )

        # Then: force-void context bounds never enter window or provider resolution.
        targets = storage.assessment_realized_targets_for(ROUND, RISK_SCHEMA_ID)
        assert result == {}
        assert {target.coordinate for target in targets} == {
            _coordinate(44, output, HORIZON_5D_SECONDS) for output in RiskOutput
        }
        assert {target.status for target in targets} == {"voided"}
        context_factory.assert_called_once_with(
            window_start_block=None,
            window_end_block=None,
            void_unavailable_targets=True,
            force_void_unavailable_targets=True,
        )
        window_start_block.assert_not_called()
        window_end_block.assert_not_called()
        provider.price_series.assert_not_called()


class TestRiskScoringOrchestrator:
    def test_transient_provider_failure_defers_all_coordinates_until_retry(
        self, storage: Storage
    ) -> None:
        fixture_provider = _provider((44,))
        provider = _FailingPriceProvider(fixture_provider, failures_remaining=None)
        _open_round(storage, (44,))
        _accept_risk_bundle(
            storage, hotkey="hk-a", netuids=(44,), provider=fixture_provider
        )
        orchestrator = _orchestrator(storage, provider)

        first = orchestrator.resolve_and_score(ROUND, HORIZON_5D_SECONDS, now_iso=NOW)

        assert first == {}
        assert not storage.assessment_realized_targets_for(ROUND, RISK_SCHEMA_ID)
        assert not storage.has_assessment_resolution_marker(
            ROUND, RISK_SCHEMA_ID, HORIZON_5D_SECONDS
        )

        provider._failures_remaining = 0
        second = orchestrator.resolve_and_score(ROUND, HORIZON_5D_SECONDS, now_iso=NOW)

        assert second["hk-a"] > Decimal(0)
        assert len(
            storage.assessment_realized_targets_for(ROUND, RISK_SCHEMA_ID)
        ) == len(tuple(RiskOutput))
        assert storage.has_assessment_resolution_marker(
            ROUND, RISK_SCHEMA_ID, HORIZON_5D_SECONDS
        )

    def test_definitively_empty_window_voids_coordinate(self, storage: Storage) -> None:
        provider = _provider((44,))
        _open_round(storage, (44, 8))
        _accept_risk_bundle(storage, hotkey="hk-a", netuids=(44,), provider=provider)
        orchestrator = _orchestrator(storage, provider)

        orchestrator.resolve_and_score(ROUND, HORIZON_5D_SECONDS, now_iso=NOW)

        targets = storage.assessment_realized_targets_for(ROUND, RISK_SCHEMA_ID)
        voided = {target.coordinate for target in targets if target.status == "voided"}
        assert voided == {
            _coordinate(8, output, HORIZON_5D_SECONDS) for output in RiskOutput
        }
        assert storage.has_assessment_resolution_marker(
            ROUND, RISK_SCHEMA_ID, HORIZON_5D_SECONDS
        )

    def test_transient_provider_failure_voids_after_grace(
        self, storage: Storage
    ) -> None:
        fixture_provider = _provider((44,))
        provider = _FailingPriceProvider(fixture_provider, failures_remaining=None)
        _open_round(storage, (44,))
        orchestrator = _orchestrator(storage, provider)
        windows = storage.round_windows(ROUND, RISK_SCHEMA_ID)
        assert windows is not None
        resolution_due_at = windows.reveal_close + timedelta(seconds=1)
        after_grace = resolution_due_at + timedelta(seconds=VOID_GRACE_SECONDS + 1)

        orchestrator.resolve_and_score(
            ROUND,
            HORIZON_5D_SECONDS,
            now_iso=after_grace.isoformat(),
            resolution_due_at=resolution_due_at,
        )

        targets = storage.assessment_realized_targets_for(ROUND, RISK_SCHEMA_ID)
        assert len(targets) == len(tuple(RiskOutput))
        assert {target.status for target in targets} == {"voided"}
        assert storage.has_assessment_resolution_marker(
            ROUND, RISK_SCHEMA_ID, HORIZON_5D_SECONDS
        )

    def test_reveal_close_lookup_failure_defers_without_writes_within_grace(
        self, storage: Storage
    ) -> None:
        # Given: the archive cannot map the persisted reveal close to a block.
        provider = _provider((44,))
        _open_round(storage, (44,))
        orchestrator = RiskScoringOrchestrator(
            storage=storage,
            price_provider=provider,
            half_life_rounds=2,
            reveal_close_block=_unavailable_reveal_close_block,
        )

        # When: resolution runs before the post-due grace deadline.
        with pytest.raises(AlphaMarketDataUnavailable):
            orchestrator.resolve_and_score(ROUND, HORIZON_5D_SECONDS, now_iso=NOW)

        # Then: no incomplete pass or resolution marker is persisted.
        assert not storage.assessment_realized_targets_for(ROUND, RISK_SCHEMA_ID)
        assert not storage.has_assessment_resolution_marker(
            ROUND, RISK_SCHEMA_ID, HORIZON_5D_SECONDS
        )

    def test_reveal_close_lookup_failure_voids_all_coordinates_after_grace(
        self, storage: Storage
    ) -> None:
        # Given: the block lookup remains unavailable beyond its void deadline.
        provider = _provider((44,))
        _open_round(storage, (44,))
        windows = storage.round_windows(ROUND, RISK_SCHEMA_ID)
        assert windows is not None
        resolution_due_at = windows.reveal_close + timedelta(seconds=1)
        after_grace = resolution_due_at + timedelta(seconds=VOID_GRACE_SECONDS + 1)
        orchestrator = RiskScoringOrchestrator(
            storage=storage,
            price_provider=provider,
            half_life_rounds=2,
            reveal_close_block=_unavailable_reveal_close_block,
        )

        # When: resolution runs after the post-due grace deadline.
        result = orchestrator.resolve_and_score(
            ROUND,
            HORIZON_5D_SECONDS,
            now_iso=after_grace.isoformat(),
            resolution_due_at=resolution_due_at,
        )

        # Then: every unresolved output coordinate is voided and finalized.
        targets = storage.assessment_realized_targets_for(ROUND, RISK_SCHEMA_ID)
        assert result == {}
        assert len(targets) == len(tuple(RiskOutput))
        assert {target.status for target in targets} == {"voided"}
        assert storage.has_assessment_resolution_marker(
            ROUND, RISK_SCHEMA_ID, HORIZON_5D_SECONDS
        )

    @pytest.mark.parametrize("missing_boundary", ["start", "end"])
    def test_missing_archive_timestamp_voids_after_grace(
        self, storage: Storage, missing_boundary: str
    ) -> None:
        # Given: a historical block exists, but its Timestamp.Now value is
        # permanently absent at either deterministic window boundary.
        provider = _provider((44,))
        _open_round(storage, (44,))
        windows = storage.round_windows(ROUND, RISK_SCHEMA_ID)
        assert windows is not None
        resolution_due_at = windows.reveal_close + timedelta(seconds=1)
        after_grace = resolution_due_at + timedelta(seconds=VOID_GRACE_SECONDS + 1)
        orchestrator = RiskScoringOrchestrator(
            storage=storage,
            price_provider=provider,
            half_life_rounds=2,
            reveal_close_block=(
                _missing_archive_timestamp
                if missing_boundary == "start"
                else lambda _timestamp: WINDOW_START_BLOCK
            ),
            window_end_block=(
                _missing_archive_timestamp if missing_boundary == "end" else None
            ),
        )

        result = orchestrator.resolve_and_score(
            ROUND,
            HORIZON_5D_SECONDS,
            now_iso=after_grace.isoformat(),
            resolution_due_at=resolution_due_at,
        )

        targets = storage.assessment_realized_targets_for(ROUND, RISK_SCHEMA_ID)
        assert result == {}
        assert len(targets) == len(tuple(RiskOutput))
        assert {target.status for target in targets} == {"voided"}
        assert storage.has_assessment_resolution_marker(
            ROUND, RISK_SCHEMA_ID, HORIZON_5D_SECONDS
        )

    @pytest.mark.parametrize("missing_boundary", ["start", "end"])
    def test_missing_archive_timestamp_defers_within_grace(
        self, storage: Storage, missing_boundary: str
    ) -> None:
        provider = _provider((44,))
        _open_round(storage, (44,))
        orchestrator = RiskScoringOrchestrator(
            storage=storage,
            price_provider=provider,
            half_life_rounds=2,
            reveal_close_block=(
                _missing_archive_timestamp
                if missing_boundary == "start"
                else lambda _timestamp: WINDOW_START_BLOCK
            ),
            window_end_block=(
                _missing_archive_timestamp if missing_boundary == "end" else None
            ),
        )

        with pytest.raises(LookupError, match="Timestamp.Now"):
            orchestrator.resolve_and_score(ROUND, HORIZON_5D_SECONDS, now_iso=NOW)

        assert not storage.assessment_realized_targets_for(ROUND, RISK_SCHEMA_ID)
        assert not storage.has_assessment_resolution_marker(
            ROUND, RISK_SCHEMA_ID, HORIZON_5D_SECONDS
        )

    def test_partial_resolution_defers_marker_until_missing_coordinate_recovers(
        self, storage: Storage
    ) -> None:
        fixture_provider = _provider((44,))
        provider = _FailingPriceProvider(fixture_provider, failures_remaining=1)
        _open_round(storage, (44,))
        _accept_risk_bundle(
            storage, hotkey="hk-a", netuids=(44,), provider=fixture_provider
        )
        orchestrator = _orchestrator(storage, provider)

        orchestrator.resolve_and_score(ROUND, HORIZON_5D_SECONDS, now_iso=NOW)

        first_targets = storage.assessment_realized_targets_for(ROUND, RISK_SCHEMA_ID)
        assert len(first_targets) == len(tuple(RiskOutput)) - 1
        assert not storage.has_assessment_resolution_marker(
            ROUND, RISK_SCHEMA_ID, HORIZON_5D_SECONDS
        )

        orchestrator.resolve_and_score(ROUND, HORIZON_5D_SECONDS, now_iso=NOW)

        targets = storage.assessment_realized_targets_for(ROUND, RISK_SCHEMA_ID)
        assert len(targets) == len(tuple(RiskOutput))
        assert storage.assessment_output_score_count(ROUND, RISK_SCHEMA_ID) == len(
            tuple(RiskOutput)
        )
        assert storage.has_assessment_resolution_marker(
            ROUND, RISK_SCHEMA_ID, HORIZON_5D_SECONDS
        )

    def test_resolver_table_scores_full_risk_bundle_end_to_end(
        self, storage: Storage
    ) -> None:
        provider = _provider((44,))
        _open_round(storage, (44,))
        _accept_risk_bundle(storage, hotkey="hk-a", netuids=(44,), provider=provider)
        orchestrator = _orchestrator(storage, provider)

        round_scores = orchestrator.resolve_and_score(
            ROUND, HORIZON_5D_SECONDS, now_iso=NOW
        )

        targets = storage.assessment_realized_targets_for(ROUND, RISK_SCHEMA_ID)
        assert len(targets) == len(tuple(RiskOutput))
        assert {target.status for target in targets} == {"resolved"}
        assert {target.coordinate.output for target in targets} == {
            output.value for output in RiskOutput
        }
        scores = storage.assessment_output_scores_for_round(ROUND, RISK_SCHEMA_ID)
        assert len(scores) == len(tuple(RiskOutput))
        assert round_scores["hk-a"] > Decimal(0)

    def test_resolves_both_horizons_for_all_eight_coordinates(
        self, storage: Storage
    ) -> None:
        provider = _provider((44,))
        _open_round(storage, (44,))
        _accept_risk_bundle(storage, hotkey="hk-a", netuids=(44,), provider=provider)
        orchestrator = _orchestrator(storage, provider)

        for horizon in RISK_HORIZONS:
            orchestrator.resolve_and_score(ROUND, horizon, now_iso=NOW)

        targets = storage.assessment_realized_targets_for(ROUND, RISK_SCHEMA_ID)
        assert len(targets) == len(tuple(RiskOutput)) * len(RISK_HORIZONS)
        assert {
            (target.coordinate.output, target.coordinate.horizon_value)
            for target in targets
        } == {
            (output.value, horizon)
            for output in RiskOutput
            for horizon in RISK_HORIZONS
        }

    def test_resolved_coordinate_skipped_by_miner_scores_zero(
        self, storage: Storage
    ) -> None:
        provider = _provider((44, 8))
        _open_round(storage, (44, 8))
        _accept_risk_bundle(storage, hotkey="hk-a", netuids=(44,), provider=provider)
        orchestrator = _orchestrator(storage, provider)

        round_scores = orchestrator.resolve_and_score(
            ROUND, HORIZON_5D_SECONDS, now_iso=NOW
        )

        scores = {
            score.coordinate: score.score
            for score in storage.assessment_output_scores_for_round(
                ROUND, RISK_SCHEMA_ID
            )
        }
        skipped_scores = [
            scores[_coordinate(8, output, HORIZON_5D_SECONDS)] for output in RiskOutput
        ]
        assert skipped_scores == [Decimal(0)] * len(tuple(RiskOutput))
        assert round_scores["hk-a"] < Decimal(1)

    def test_recorded_mainnet_fixture_resolves_both_horizons_for_both_netuids(
        self, storage: Storage
    ) -> None:
        provider = recorded_mainnet_fixture_provider()
        _open_round(storage, (44, 8))
        orchestrator = RiskScoringOrchestrator(
            storage=storage,
            price_provider=provider,
            half_life_rounds=2,
            reveal_close_block=lambda reveal_close: RECORDED_FIXTURE_ANCHOR_BLOCK,
        )

        for horizon in RISK_HORIZONS:
            orchestrator.resolve_and_score(ROUND, horizon, now_iso=NOW)

        targets = storage.assessment_realized_targets_for(ROUND, RISK_SCHEMA_ID)
        assert len(targets) == 2 * len(tuple(RiskOutput)) * len(RISK_HORIZONS)
        assert {target.status for target in targets} == {"resolved"}
        by_coordinate = {target.coordinate: target for target in targets}
        for netuid in (44, 8):
            series = provider.price_series(
                netuid,
                window=ResolutionWindow(
                    start_block=RECORDED_FIXTURE_ANCHOR_BLOCK,
                    horizon_blocks=horizon_seconds_to_blocks(max(RISK_HORIZONS)),
                ),
            )
            assert series is not None
            for horizon in RISK_HORIZONS:
                horizon_blocks = horizon_seconds_to_blocks(horizon)
                window = filter_snapshots_for_window(
                    series.snapshots,
                    window_start_block=RECORDED_FIXTURE_ANCHOR_BLOCK,
                    horizon_blocks=horizon_blocks,
                )
                prices = tuple(
                    int(snapshot.price_tao_per_alpha * Decimal(1_000_000_000))
                    for snapshot in window
                )
                reserves = tuple(snapshot.tao_reserve_rao for snapshot in window)
                drawdown = by_coordinate[
                    _coordinate(netuid, RiskOutput.MAX_DRAWDOWN, horizon)
                ].value
                volatility = by_coordinate[
                    _coordinate(netuid, RiskOutput.REALIZED_VOLATILITY, horizon)
                ].value
                twap = by_coordinate[
                    _coordinate(netuid, RiskOutput.TWAP_PRICE, horizon)
                ].value
                depth = by_coordinate[
                    _coordinate(netuid, RiskOutput.LIQUIDITY_DEPTH, horizon)
                ].value
                assert drawdown is not None and Decimal(0) <= drawdown <= Decimal(
                    10_000
                )
                assert volatility is not None and volatility > 0
                assert twap is not None and min(prices) <= twap <= max(prices)
                assert depth is not None and min(reserves) <= depth <= max(reserves)


WEEKDAYS_JULY_2026 = (7, 8, 9, 10, 13, 14, 15, 16, 17, 20, 21, 22, 23, 24)


def _round_for(day: int) -> tuple[str, str]:
    round_date = date(2026, 7, day)
    return round_date.isoformat(), datetime(2026, 7, day, 12, 0, tzinfo=UTC).isoformat()


def _open_round_for(storage: Storage, round_id: str, netuids: tuple[int, ...]) -> None:
    storage.open_round(
        windows=compute_windows(date.fromisoformat(round_id), offsets=DEFAULT_OFFSETS),
        schema_id=RISK_SCHEMA_ID,
        universe=StaticAlphaRiskUniverseProvider(netuids=netuids).fetch_universe(
            round_id
        ),
        now_iso=NOW,
    )


def _accept_bundle_for_round(
    storage: Storage,
    round_id: str,
    now_iso: str,
    *,
    hotkey: str,
    netuids: tuple[int, ...],
    provider: FixtureAlphaPriceProvider,
) -> None:
    bundle = baseline_risk_bundle(
        round_id=round_id,
        netuids=netuids,
        latest_observation=_latest_for(provider),
    )
    bundle_json = canonical_bundle_bytes(bundle.to_canonical_payload()).decode("utf-8")
    storage.record_commit(round_id, RISK_SCHEMA_ID, hotkey, "ab" * 32, now_iso=now_iso)
    storage.record_reveal(
        round_id,
        RISK_SCHEMA_ID,
        hotkey,
        bundle_json=bundle_json,
        nonce_hex="cd" * 16,
        accepted=True,
        rejection_code=None,
        now_iso=now_iso,
    )


def _fast_decay_orchestrator(
    storage: Storage,
    provider: AlphaPriceProvider,
    *,
    registered_hotkeys: Callable[[], list[str]] | None = None,
) -> RiskScoringOrchestrator:
    return RiskScoringOrchestrator(
        storage=storage,
        price_provider=provider,
        half_life_rounds=1,
        reveal_close_block=lambda reveal_close: WINDOW_START_BLOCK,
        registered_hotkeys=registered_hotkeys,
    )


def _ema_map(storage: Storage, hotkey: str) -> dict[AssessmentCoordinate, Decimal]:
    return {
        state.coordinate: state.ema
        for state in storage.assessment_ema_states(RISK_SCHEMA_ID)
        if state.miner_hotkey == hotkey
    }


class TestAbsenceAwareScoring:
    def _score_two_rounds(
        self, storage: Storage, provider: FixtureAlphaPriceProvider
    ) -> RiskScoringOrchestrator:
        orchestrator = _fast_decay_orchestrator(storage, provider)
        round_one, now_one = _round_for(6)
        _open_round_for(storage, round_one, (44,))
        for hotkey in ("hk-a", "hk-b"):
            _accept_bundle_for_round(
                storage,
                round_one,
                now_one,
                hotkey=hotkey,
                netuids=(44,),
                provider=provider,
            )
        orchestrator.resolve_and_score(round_one, HORIZON_5D_SECONDS, now_iso=now_one)
        return orchestrator

    def test_absent_miner_receives_zero_observations(self, storage: Storage) -> None:
        provider = _provider((44,))
        orchestrator = self._score_two_rounds(storage, provider)
        before = _ema_map(storage, "hk-b")
        assert before

        round_two, now_two = _round_for(7)
        _open_round_for(storage, round_two, (44,))
        _accept_bundle_for_round(
            storage, round_two, now_two, hotkey="hk-a", netuids=(44,), provider=provider
        )
        round_scores = orchestrator.resolve_and_score(
            round_two, HORIZON_5D_SECONDS, now_iso=now_two
        )

        assert round_scores["hk-b"] == Decimal(0)
        after = _ema_map(storage, "hk-b")
        for coordinate, ema_before in before.items():
            if ema_before > Decimal(0):
                assert after[coordinate] < ema_before
        history = storage.assessment_score_history_for_round(round_two, RISK_SCHEMA_ID)
        absent_rows = [row for row in history if row.miner_hotkey == "hk-b"]
        assert absent_rows
        assert {row.round_score for row in absent_rows} == {Decimal(0)}

    def test_never_submitter_accrues_no_state(self, storage: Storage) -> None:
        provider = _provider((44,))
        orchestrator = self._score_two_rounds(storage, provider)
        hotkeys = {
            state.miner_hotkey
            for state in storage.assessment_ema_states(RISK_SCHEMA_ID)
        }
        assert hotkeys == {"hk-a", "hk-b"}
        assert orchestrator.blended_scores().keys() == {"hk-a", "hk-b"}

    def test_later_joiner_is_not_zero_filled_into_an_older_long_horizon(
        self, storage: Storage
    ) -> None:
        provider = _provider((44,))
        orchestrator = _fast_decay_orchestrator(storage, provider)
        old_round, old_now = _round_for(7)
        _open_round_for(storage, old_round, (44,))
        _accept_bundle_for_round(
            storage,
            old_round,
            old_now,
            hotkey="hk-incumbent",
            netuids=(44,),
            provider=provider,
        )

        join_round, join_now = _round_for(8)
        _open_round_for(storage, join_round, (44,))
        for hotkey in ("hk-incumbent", "hk-newcomer"):
            _accept_bundle_for_round(
                storage,
                join_round,
                join_now,
                hotkey=hotkey,
                netuids=(44,),
                provider=provider,
            )
        orchestrator.resolve_and_score(join_round, HORIZON_5D_SECONDS, now_iso=join_now)

        round_scores = orchestrator.resolve_and_score(
            old_round, RISK_HORIZONS[1], now_iso=old_now
        )

        assert "hk-newcomer" not in round_scores
        old_history = storage.assessment_score_history_for_round(
            old_round, RISK_SCHEMA_ID
        )
        assert not [row for row in old_history if row.miner_hotkey == "hk-newcomer"]

    def test_staggered_horizon_absence_decays_only_resolved_horizon(
        self, storage: Storage
    ) -> None:
        provider = _provider((44,))
        orchestrator = self._score_two_rounds(storage, provider)
        round_one, now_one = _round_for(6)
        orchestrator.resolve_and_score(round_one, RISK_HORIZONS[1], now_iso=now_one)
        before = _ema_map(storage, "hk-b")
        horizon_30d = RISK_HORIZONS[1]

        round_two, now_two = _round_for(7)
        _open_round_for(storage, round_two, (44,))
        _accept_bundle_for_round(
            storage, round_two, now_two, hotkey="hk-a", netuids=(44,), provider=provider
        )
        orchestrator.resolve_and_score(round_two, HORIZON_5D_SECONDS, now_iso=now_two)

        after = _ema_map(storage, "hk-b")
        for coordinate, ema_before in before.items():
            if coordinate.horizon_value == horizon_30d:
                assert after[coordinate] == ema_before
            elif ema_before > Decimal(0):
                assert after[coordinate] < ema_before

    def test_confirmed_deregistered_archived_and_cold_started(
        self, storage: Storage
    ) -> None:
        provider = _provider((44,))
        orchestrator = self._score_two_rounds(storage, provider)

        round_two, now_two = _round_for(7)
        _open_round_for(storage, round_two, (44,))
        _accept_bundle_for_round(
            storage, round_two, now_two, hotkey="hk-a", netuids=(44,), provider=provider
        )
        orchestrator.resolve_and_score(
            round_two,
            HORIZON_5D_SECONDS,
            now_iso=now_two,
            archive_hotkeys=("hk-b",),
        )

        assert not _ema_map(storage, "hk-b")
        history = storage.assessment_score_history_for_round(round_two, RISK_SCHEMA_ID)
        final_zero_fill = [row for row in history if row.miner_hotkey == "hk-b"]
        assert final_zero_fill
        assert {row.round_score for row in final_zero_fill} == {Decimal(0)}

        round_three, now_three = _round_for(8)
        _open_round_for(storage, round_three, (44,))
        _accept_bundle_for_round(
            storage,
            round_three,
            now_three,
            hotkey="hk-b",
            netuids=(44,),
            provider=provider,
        )
        orchestrator.resolve_and_score(
            round_three, HORIZON_5D_SECONDS, now_iso=now_three
        )
        reborn = _ema_map(storage, "hk-b")
        assert reborn
        history_three = storage.assessment_score_history_for_round(
            round_three, RISK_SCHEMA_ID
        )
        rows_by_coordinate = {
            row.coordinate: row for row in history_three if row.miner_hotkey == "hk-b"
        }
        for coordinate, ema in reborn.items():
            if coordinate in rows_by_coordinate:
                assert ema == rows_by_coordinate[coordinate].ema_after

    def test_fully_decayed_absent_hotkey_is_pruned(self, storage: Storage) -> None:
        provider = _provider((44,))
        orchestrator = self._score_two_rounds(storage, provider)
        assert _ema_map(storage, "hk-b")

        pruned_after: int | None = None
        for index, day in enumerate(WEEKDAYS_JULY_2026[:-1]):
            round_id, now_iso = _round_for(day)
            _open_round_for(storage, round_id, (44,))
            _accept_bundle_for_round(
                storage,
                round_id,
                now_iso,
                hotkey="hk-a",
                netuids=(44,),
                provider=provider,
            )
            orchestrator.resolve_and_score(
                round_id, HORIZON_5D_SECONDS, now_iso=now_iso
            )
            if not _ema_map(storage, "hk-b"):
                pruned_after = index
                break
        assert pruned_after is not None

        follow_up, follow_now = _round_for(WEEKDAYS_JULY_2026[pruned_after + 1])
        _open_round_for(storage, follow_up, (44,))
        _accept_bundle_for_round(
            storage,
            follow_up,
            follow_now,
            hotkey="hk-a",
            netuids=(44,),
            provider=provider,
        )
        orchestrator.resolve_and_score(
            follow_up, HORIZON_5D_SECONDS, now_iso=follow_now
        )
        history = storage.assessment_score_history_for_round(follow_up, RISK_SCHEMA_ID)
        assert not [row for row in history if row.miner_hotkey == "hk-b"]
        assert _ema_map(storage, "hk-a")

    def test_submitter_is_never_pruned(self, storage: Storage) -> None:
        provider = _provider((44,))
        orchestrator = self._score_two_rounds(storage, provider)

        for day in WEEKDAYS_JULY_2026:
            round_id, now_iso = _round_for(day)
            _open_round_for(storage, round_id, (44,))
            _accept_bundle_for_round(
                storage,
                round_id,
                now_iso,
                hotkey="hk-a",
                netuids=(44,),
                provider=provider,
            )
            orchestrator.resolve_and_score(
                round_id, HORIZON_5D_SECONDS, now_iso=now_iso
            )
        assert _ema_map(storage, "hk-a")

    def test_voided_cells_leave_absent_miner_untouched(self, storage: Storage) -> None:
        provider = _provider((44,))
        orchestrator = self._score_two_rounds(storage, provider)
        before = _ema_map(storage, "hk-b")

        round_two, now_two = _round_for(7)
        _open_round_for(storage, round_two, (8,))
        orchestrator.resolve_and_score(round_two, HORIZON_5D_SECONDS, now_iso=now_two)

        assert _ema_map(storage, "hk-b") == before
        history = storage.assessment_score_history_for_round(round_two, RISK_SCHEMA_ID)
        assert not history

    def test_weights_filter_registered_only(self, storage: Storage) -> None:
        provider = _provider((44,))
        registered = ["hk-a", "hk-b"]
        orchestrator = _fast_decay_orchestrator(
            storage, provider, registered_hotkeys=lambda: registered
        )
        round_one, now_one = _round_for(6)
        _open_round_for(storage, round_one, (44,))
        for hotkey in ("hk-a", "hk-b"):
            _accept_bundle_for_round(
                storage,
                round_one,
                now_one,
                hotkey=hotkey,
                netuids=(44,),
                provider=provider,
            )
        orchestrator.resolve_and_score(round_one, HORIZON_5D_SECONDS, now_iso=now_one)
        assert set(orchestrator.weights()) == {"hk-a", "hk-b"}

        registered.remove("hk-b")
        assert set(orchestrator.weights()) == {"hk-a"}
        assert set(orchestrator.blended_scores()) == {"hk-a", "hk-b"}

    def test_archived_hotkey_with_accepted_submission_is_scored_then_archived(
        self, storage: Storage
    ) -> None:
        provider = _provider((44,))
        orchestrator = self._score_two_rounds(storage, provider)

        round_two, now_two = _round_for(7)
        _open_round_for(storage, round_two, (44,))
        for hotkey in ("hk-a", "hk-b"):
            _accept_bundle_for_round(
                storage,
                round_two,
                now_two,
                hotkey=hotkey,
                netuids=(44,),
                provider=provider,
            )
        round_scores = orchestrator.resolve_and_score(
            round_two,
            HORIZON_5D_SECONDS,
            now_iso=now_two,
            archive_hotkeys=("hk-b",),
        )

        assert round_scores["hk-b"] > Decimal(0)
        history = storage.assessment_score_history_for_round(round_two, RISK_SCHEMA_ID)
        scored_rows = [row for row in history if row.miner_hotkey == "hk-b"]
        assert scored_rows
        assert any(row.round_score > Decimal(0) for row in scored_rows)
        assert not _ema_map(storage, "hk-b")

    def test_void_only_pass_leaves_low_ema_absent_hotkey_unpruned(
        self, storage: Storage
    ) -> None:
        provider = _provider((44,))
        orchestrator = _fast_decay_orchestrator(storage, provider)
        round_one, now_one = _round_for(6)
        _open_round_for(storage, round_one, (44,))
        _accept_bundle_for_round(
            storage, round_one, now_one, hotkey="hk-a", netuids=(44,), provider=provider
        )
        orchestrator.resolve_and_score(round_one, HORIZON_5D_SECONDS, now_iso=now_one)
        coordinate = _coordinate(44, RiskOutput.MAX_DRAWDOWN, HORIZON_5D_SECONDS)
        storage.record_assessment_scoring_pass(
            round_one,
            RISK_SCHEMA_ID,
            horizon_value=RISK_HORIZONS[1],
            realized_targets=[],
            output_scores=[],
            ema_updates=[
                AssessmentEmaState(
                    miner_hotkey="hk-b",
                    coordinate=coordinate,
                    ema=Decimal("0.005"),
                    resolved_rounds=1,
                )
            ],
            score_history=[],
            complete=False,
            now_iso=now_one,
        )
        assert _ema_map(storage, "hk-b") == {coordinate: Decimal("0.005")}

        round_two, now_two = _round_for(7)
        _open_round_for(storage, round_two, (8,))
        orchestrator.resolve_and_score(round_two, HORIZON_5D_SECONDS, now_iso=now_two)

        assert _ema_map(storage, "hk-b") == {coordinate: Decimal("0.005")}


class TestRiskScoringInputGuards:
    """Unusable scoring inputs fail loudly or void, never score silently."""

    def test_unparseable_accepted_bundle_skips_only_that_miner(
        self, storage: Storage
    ) -> None:
        # An accepted reveal that no longer parses is storage corruption for one
        # miner; scoring must drop that miner and still score the healthy one.
        netuids = (44,)
        provider = _provider(netuids)
        _open_round(storage, netuids)
        _accept_risk_bundle(
            storage, hotkey="hk-good", netuids=netuids, provider=provider
        )
        storage.record_commit(
            ROUND, RISK_SCHEMA_ID, "hk-corrupt", "ef" * 32, now_iso=NOW
        )
        storage.record_reveal(
            ROUND,
            RISK_SCHEMA_ID,
            "hk-corrupt",
            bundle_json='{"round_id":"2026-07-06","schema_id":"risk.v1.subnet_alpha"}',
            nonce_hex="ab" * 16,
            accepted=True,
            rejection_code=None,
            now_iso=NOW,
        )

        values = risk_orchestrator_module.accepted_risk_values(storage, ROUND)

        assert set(values) == {"hk-good"}
        assert values["hk-good"]

    def test_round_without_stored_windows_is_refused(self, storage: Storage) -> None:
        orchestrator = _orchestrator(storage, _provider((44,)))

        with pytest.raises(AlphaMarketDataError, match="has no stored windows"):
            orchestrator.resolve_and_score(
                "2999-01-01", HORIZON_5D_SECONDS, now_iso=NOW
            )

    @pytest.mark.parametrize(
        ("now_iso", "resolution_due_at", "message"),
        [
            pytest.param(
                "2026-07-06T12:00:00",
                None,
                "now_iso must be timezone-aware",
                id="naive-now",
            ),
            pytest.param(
                NOW,
                datetime(2026, 7, 11, 12, 0),
                "resolution due time must be timezone-aware",
                id="naive-resolution-due",
            ),
        ],
    )
    def test_naive_timestamps_are_refused(
        self,
        storage: Storage,
        now_iso: str,
        resolution_due_at: datetime | None,
        message: str,
    ) -> None:
        # A naive timestamp would silently compare against UTC and could void a
        # whole round's worth of coordinates.
        netuids = (44,)
        _open_round(storage, netuids)
        orchestrator = _orchestrator(storage, _provider(netuids))

        with pytest.raises(AlphaMarketDataError, match=message):
            orchestrator.resolve_and_score(
                ROUND,
                HORIZON_5D_SECONDS,
                now_iso=now_iso,
                resolution_due_at=resolution_due_at,
            )

    def test_netuid_absent_from_the_provider_voids_its_coordinates(
        self, storage: Storage
    ) -> None:
        # The universe carries a netuid the provider has no series for: those
        # coordinates void rather than resolving to a fabricated value.
        netuids = (44, 8)
        provider = _provider((44,))
        _open_round(storage, netuids)
        _accept_risk_bundle(
            storage, hotkey="hk-a", netuids=netuids, provider=_provider(netuids)
        )
        orchestrator = _orchestrator(storage, provider)

        orchestrator.resolve_and_score(ROUND, HORIZON_5D_SECONDS, now_iso=NOW)

        targets = {
            target.coordinate: target
            for target in storage.assessment_realized_targets_for(ROUND, RISK_SCHEMA_ID)
        }
        absent = [
            target
            for coordinate, target in targets.items()
            if coordinate.target_id == "8"
        ]
        assert absent
        assert all(target.status == "voided" for target in absent)
        assert all(target.value is None for target in absent)

    def test_archive_outage_past_grace_voids_instead_of_raising(
        self, storage: Storage
    ) -> None:
        # The block lookup is unavailable and the void grace has expired, so the
        # round must settle as voided rather than wedging on a raise.
        netuids = (44,)
        _open_round(storage, netuids)
        _accept_risk_bundle(
            storage, hotkey="hk-a", netuids=netuids, provider=_provider(netuids)
        )
        orchestrator = RiskScoringOrchestrator(
            storage=storage,
            price_provider=_provider(netuids),
            half_life_rounds=2,
            reveal_close_block=_unavailable_reveal_close_block,
        )
        windows = storage.round_windows(ROUND, RISK_SCHEMA_ID)
        assert windows is not None
        past_grace = windows.reveal_close + timedelta(
            seconds=HORIZON_5D_SECONDS + VOID_GRACE_SECONDS + 60
        )

        orchestrator.resolve_and_score(
            ROUND, HORIZON_5D_SECONDS, now_iso=past_grace.isoformat()
        )

        targets = storage.assessment_realized_targets_for(ROUND, RISK_SCHEMA_ID)
        assert targets
        assert all(target.status == "voided" for target in targets)

    def test_archive_outage_within_grace_refuses_to_score(
        self, storage: Storage
    ) -> None:
        # Same outage before the grace expires must surface, so a transient
        # archive gap never silently voids a scoreable round.
        netuids = (44,)
        _open_round(storage, netuids)
        orchestrator = RiskScoringOrchestrator(
            storage=storage,
            price_provider=_provider(netuids),
            half_life_rounds=2,
            reveal_close_block=_unavailable_reveal_close_block,
        )

        with pytest.raises(AlphaMarketDataUnavailable):
            orchestrator.resolve_and_score(ROUND, HORIZON_5D_SECONDS, now_iso=NOW)

    def test_non_advancing_resolution_window_is_refused(self, storage: Storage) -> None:
        # A window whose end does not follow its start would make every
        # observable degenerate; the resolver must refuse it outright.
        netuids = (44,)
        _open_round(storage, netuids)
        _accept_risk_bundle(
            storage, hotkey="hk-a", netuids=netuids, provider=_provider(netuids)
        )
        orchestrator = RiskScoringOrchestrator(
            storage=storage,
            price_provider=_provider(netuids),
            half_life_rounds=2,
            reveal_close_block=lambda _reveal_close: WINDOW_START_BLOCK,
            window_end_block=lambda _window_end: WINDOW_START_BLOCK,
        )

        with pytest.raises(AlphaMarketDataError, match="positive block span"):
            orchestrator.resolve_and_score(ROUND, HORIZON_5D_SECONDS, now_iso=NOW)
