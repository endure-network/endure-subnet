"""Alpha Risk V1 scoring config (risk scope spec §Scoring)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final, assert_never

import bittensor as bt
from pydantic import ValidationError

from endure.assessment.coordinates import AssessmentCoordinate, AssessmentRealizedTarget
from endure.assessment.schemas.subnet_alpha_risk import (
    RISK_HORIZONS,
    RISK_SCHEMA_ID,
    RISK_SPECS_BY_OUTPUT,
    RiskOutput,
    RiskSubmissionBundle,
)
from endure.assessment.subnet_alpha_universe import parse_alpha_risk_universe_members
from endure.scoring.assessment_orchestrator import (
    REALIZED_TARGET_RESOLVED,
    REALIZED_TARGET_VOIDED,
    AssessmentResolutionContext,
    AssessmentScoringConfig,
    AssessmentScoringOrchestrator,
    ScoredOutputConfig,
    submitted_assessment_values,
)
from endure.scoring.market_data import (
    AlphaMarketDataError,
    AlphaPriceProvider,
    AlphaPriceSnapshot,
    ResolutionWindow,
)
from endure.scoring.risk.observables import (
    filter_snapshots_for_window,
    horizon_seconds_to_blocks,
    liquidity_depth_rao,
    max_drawdown_bps,
    realized_volatility_bps,
    should_void_realized_window,
    twap_price_rao,
)
from endure.storage.repository import Storage

RevealCloseBlock = Callable[[datetime], int]
WindowEndBlock = Callable[[datetime], int]
VOID_GRACE_SECONDS: Final = 24 * 60 * 60


def risk_coordinate(
    netuid: int, horizon: int, output: RiskOutput
) -> AssessmentCoordinate:
    return AssessmentCoordinate.subnet_asset(
        netuid=netuid, horizon_seconds=horizon, output=output.value
    )


def accepted_risk_values(
    storage: Storage, round_id: str
) -> dict[str, dict[AssessmentCoordinate, int]]:
    """Extract all eight Alpha Risk values per asset for risk scope §Scoring."""
    values: dict[str, dict[AssessmentCoordinate, int]] = {}
    for accepted in storage.accepted_assessment_bundles(round_id, RISK_SCHEMA_ID):
        try:
            bundle = RiskSubmissionBundle.model_validate_json(accepted.bundle_json)
        except ValidationError as error:
            bt.logging.error(
                f"accepted risk bundle for {accepted.miner_hotkey} failed to parse "
                f"during scoring — skipping miner: {error}"
            )
            continue
        values[accepted.miner_hotkey] = submitted_assessment_values(
            bundle.assets, risk_coordinate
        )
    return values


class RiskScoringOrchestrator(AssessmentScoringOrchestrator):
    """Thin Alpha Risk config over the generic assessment orchestrator."""

    def __init__(
        self,
        *,
        storage: Storage,
        price_provider: AlphaPriceProvider,
        half_life_rounds: int,
        reveal_close_block: RevealCloseBlock,
        window_end_block: WindowEndBlock | None = None,
        registered_hotkeys: Callable[[], Sequence[str]] | None = None,
    ) -> None:
        self._storage_for_window = storage
        self._reveal_close_block = reveal_close_block
        self._window_end_block_lookup = window_end_block
        super().__init__(
            storage=storage,
            config=build_risk_scoring_config(
                storage=storage,
                price_provider=price_provider,
            ),
            half_life_rounds=half_life_rounds,
            registered_hotkeys=registered_hotkeys,
        )

    def resolve_and_score(
        self,
        round_id: str,
        horizon: int | None = None,
        *,
        now_iso: str,
        resolution_due_at: datetime | None = None,
        archive_hotkeys: Sequence[str] = (),
    ) -> dict[str, Decimal]:
        windows = self._storage_for_window.round_windows(round_id, RISK_SCHEMA_ID)
        if windows is None:
            raise AlphaMarketDataError(f"risk round {round_id} has no stored windows")
        scoring_horizon = self.horizons[0] if horizon is None else horizon
        now = datetime.fromisoformat(now_iso)
        if now.tzinfo is None or now.utcoffset() is None:
            raise AlphaMarketDataError("risk scoring now_iso must be timezone-aware")
        resolution_due = resolution_due_at or (
            windows.reveal_close + timedelta(seconds=scoring_horizon)
        )
        if resolution_due.tzinfo is None or resolution_due.utcoffset() is None:
            raise AlphaMarketDataError(
                "risk resolution due time must be timezone-aware"
            )
        void_unavailable_targets = now > resolution_due + timedelta(
            seconds=VOID_GRACE_SECONDS
        )
        force_void_unavailable_targets = False
        try:
            window_start_block = self._reveal_close_block(windows.reveal_close)
            if self._window_end_block_lookup is None:
                window_end_block = window_start_block + horizon_seconds_to_blocks(
                    scoring_horizon
                )
            else:
                window_end_block = self._window_end_block_lookup(
                    windows.reveal_close + timedelta(seconds=scoring_horizon)
                )
        except (AlphaMarketDataError, ConnectionError):
            if not void_unavailable_targets:
                raise
            force_void_unavailable_targets = True
            window_start_block = None
            window_end_block = None
        context = AssessmentResolutionContext(
            window_start_block=window_start_block,
            window_end_block=window_end_block,
            void_unavailable_targets=void_unavailable_targets,
            force_void_unavailable_targets=force_void_unavailable_targets,
        )
        return super().resolve_and_score(
            round_id,
            scoring_horizon,
            now_iso=now_iso,
            resolution_due_at=resolution_due,
            archive_hotkeys=archive_hotkeys,
            context=context,
        )


def build_risk_scoring_config(
    *,
    storage: Storage,
    price_provider: AlphaPriceProvider,
) -> AssessmentScoringConfig:
    """Build resolver-table config for risk.v1.subnet_alpha (risk scope §Scoring)."""
    resolvers = _risk_resolvers(price_provider)
    return AssessmentScoringConfig(
        schema_id=RISK_SCHEMA_ID,
        horizons=RISK_HORIZONS,
        universe_members=parse_alpha_risk_universe_members,
        accepted_values=lambda round_id: accepted_risk_values(storage, round_id),
        outputs=tuple(
            ScoredOutputConfig(
                output=output.value,
                resolver=resolvers[output],
                spec=RISK_SPECS_BY_OUTPUT[output],
            )
            for output in RiskOutput
        ),
    )


def _risk_resolvers(
    price_provider: AlphaPriceProvider,
) -> Mapping[
    RiskOutput,
    Callable[[AssessmentResolutionContext, int, int], AssessmentRealizedTarget | None],
]:
    return {
        output: _resolver_for_output(
            output,
            price_provider=price_provider,
        )
        for output in RiskOutput
    }


def _resolver_for_output(
    output: RiskOutput,
    *,
    price_provider: AlphaPriceProvider,
) -> Callable[[AssessmentResolutionContext, int, int], AssessmentRealizedTarget | None]:
    def resolve(
        context: AssessmentResolutionContext, netuid: int, horizon: int
    ) -> AssessmentRealizedTarget | None:
        coordinate = risk_coordinate(netuid, horizon, output)
        if context.force_void_unavailable_targets:
            return AssessmentRealizedTarget(
                coordinate=coordinate, value=None, status=REALIZED_TARGET_VOIDED
            )
        start_block = context.window_start_block
        end_block = context.window_end_block
        if start_block is None or end_block is None or end_block <= start_block:
            raise AlphaMarketDataError(
                "risk resolver window must have positive block span"
            )
        horizon_blocks = end_block - start_block
        try:
            series = price_provider.price_series(
                netuid,
                window=ResolutionWindow(
                    start_block=start_block, horizon_blocks=horizon_blocks
                ),
            )
        except AlphaMarketDataError:
            return _unavailable_target(
                coordinate,
                void_unavailable_targets=context.void_unavailable_targets,
            )
        if series is None:
            return AssessmentRealizedTarget(
                coordinate=coordinate, value=None, status=REALIZED_TARGET_VOIDED
            )
        window = filter_snapshots_for_window(
            series.snapshots,
            window_start_block=start_block,
            horizon_blocks=horizon_blocks,
        )
        if should_void_realized_window(window, horizon_blocks=horizon_blocks):
            return AssessmentRealizedTarget(
                coordinate=coordinate,
                value=None,
                status=REALIZED_TARGET_VOIDED,
                provider_payload_hash=series.payload_hash,
            )
        try:
            value = _resolve_output_value(
                output,
                window=window,
                window_start_block=start_block,
            )
        except AlphaMarketDataError:
            return AssessmentRealizedTarget(
                coordinate=coordinate,
                value=None,
                status=REALIZED_TARGET_VOIDED,
                provider_payload_hash=series.payload_hash,
            )
        return AssessmentRealizedTarget(
            coordinate=coordinate,
            value=Decimal(value),
            status=REALIZED_TARGET_RESOLVED,
            provider_payload_hash=series.payload_hash,
        )

    return resolve


def _unavailable_target(
    coordinate: AssessmentCoordinate,
    *,
    void_unavailable_targets: bool,
    provider_payload_hash: str | None = None,
) -> AssessmentRealizedTarget | None:
    if not void_unavailable_targets:
        return None
    return AssessmentRealizedTarget(
        coordinate=coordinate,
        value=None,
        status=REALIZED_TARGET_VOIDED,
        provider_payload_hash=provider_payload_hash,
    )


def _resolve_output_value(
    output: RiskOutput,
    *,
    window: tuple[AlphaPriceSnapshot, ...],
    window_start_block: int,
) -> int:
    match output:
        case RiskOutput.MAX_DRAWDOWN:
            return max_drawdown_bps(window)
        case RiskOutput.REALIZED_VOLATILITY:
            return realized_volatility_bps(window)
        case RiskOutput.TWAP_PRICE:
            return twap_price_rao(window, window_start_block=window_start_block)
        case RiskOutput.LIQUIDITY_DEPTH:
            return liquidity_depth_rao(window, window_start_block=window_start_block)
        case unreachable:
            assert_never(unreachable)
