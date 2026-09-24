"""Offline, as-of-block comparison of the adaptive and reference risk miners.

The bundled fixture has only 35 days for two pools. It can characterize 5-day
forecasts, but has no complete 30-day lookback followed by a 30-day outcome.
The default 13-hour block offset approximates the interval from the miner's
11:00 UTC commit open to the next 00:00 UTC reveal close. Actual chain block
timestamps and miner assembly time can differ, so this is exploratory rather
than a replay of production rounds. Overlapping samples also do not prove
future reward performance.

For a longer local dataset, pass ``--input-json`` with this shape (prices must
be decimal strings):

    {"8": [[7395600, "0.043504750", 92281489419376], ...], ...}

The input is read only. This script never changes the canonical fixture.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from custom_miner.adaptive import ADAPTIVE_REASON_CODE, adaptive_risk_bundle
from endure.assessment.schemas.subnet_alpha_risk import (
    HORIZON_30D_SECONDS,
    RISK_HORIZONS,
    RISK_SPECS_BY_OUTPUT,
    RiskOutput,
    RiskSubmissionBundle,
)
from endure.protocol.risk_miner import BASELINE_BPS_VALUES
from endure.scoring.assessment_orchestrator import score_output
from endure.scoring.market_data import (
    AlphaMarketDataError,
    AlphaPriceSeries,
    AlphaPriceSnapshot,
)
from endure.scoring.recorded_fixtures.alpha_mainnet import RECORDED_ALPHA_MAINNET_ROWS
from endure.scoring.risk.observables import (
    horizon_seconds_to_blocks,
    liquidity_depth_rao,
    max_drawdown_bps,
    realized_volatility_bps,
    should_void_realized_window,
    twap_price_rao,
)

DAY_BLOCKS = horizon_seconds_to_blocks(24 * 60 * 60)
DEFAULT_OUTCOME_LAG_BLOCKS = horizon_seconds_to_blocks(13 * 60 * 60)
MAX_LOOKBACK_BLOCKS = horizon_seconds_to_blocks(HORIZON_30D_SECONDS)
SNAPSHOT_FIELDS = 3
BACKTEST_ROUND_ID = "2000-01-01"
type RecordedRows = Mapping[int, Sequence[tuple[int, str, int]]]


@dataclass(slots=True)
class ScoreTotals:
    adaptive: Decimal = Decimal(0)
    reference: Decimal = Decimal(0)
    eligible_samples: int = 0
    adaptive_history_samples: int = 0

    def add(self, *, adaptive: Decimal, reference: Decimal, used_history: bool) -> None:
        self.adaptive += adaptive
        self.reference += reference
        self.eligible_samples += 1
        self.adaptive_history_samples += int(used_history)

    def report(self) -> dict[str, int | str | None]:
        count = self.eligible_samples
        return {
            "eligible_samples": count,
            "adaptive_history_samples": self.adaptive_history_samples,
            "fallback_samples": count - self.adaptive_history_samples,
            "adaptive_mean_score": str(self.adaptive / count) if count else None,
            "reference_mean_score": str(self.reference / count) if count else None,
            "adaptive_minus_reference": (
                str((self.adaptive - self.reference) / count) if count else None
            ),
        }


def load_json_rows(path: Path) -> dict[int, tuple[tuple[int, str, int], ...]]:
    """Read a local snapshot dataset without accepting float price values."""
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("input JSON must map netuid strings to snapshot arrays")
    parsed: dict[int, tuple[tuple[int, str, int], ...]] = {}
    for netuid_text, raw_rows in raw.items():
        if (
            not isinstance(netuid_text, str)
            or not netuid_text.isascii()
            or not netuid_text.isdecimal()
            or str(int(netuid_text)) != netuid_text
            or not isinstance(raw_rows, list)
        ):
            raise ValueError("each input key must be a canonical netuid string")
        rows: list[tuple[int, str, int]] = []
        for raw_row in raw_rows:
            if not isinstance(raw_row, list) or len(raw_row) != SNAPSHOT_FIELDS:
                raise ValueError("each snapshot must be [block, price, reserve]")
            block, price, reserve = raw_row
            if (
                isinstance(block, bool)
                or not isinstance(block, int)
                or not isinstance(price, str)
                or isinstance(reserve, bool)
                or not isinstance(reserve, int)
            ):
                raise ValueError(
                    "snapshot block/reserve must be integers; price a string"
                )
            rows.append((block, price, reserve))
        parsed[int(netuid_text)] = tuple(rows)
    return parsed


def _snapshots(
    netuid: int, rows: Sequence[tuple[int, str, int]]
) -> tuple[AlphaPriceSnapshot, ...]:
    try:
        snapshots = tuple(
            AlphaPriceSnapshot(
                netuid=netuid,
                block=block,
                price_tao_per_alpha=Decimal(price),
                tao_reserve_rao=reserve,
            )
            for block, price, reserve in rows
        )
    except (AlphaMarketDataError, InvalidOperation) as error:
        raise ValueError(f"invalid snapshots for netuid {netuid}: {error}") from error
    if not snapshots:
        raise ValueError(f"netuid {netuid} has no snapshots")
    AlphaPriceSeries(source="backtest_input", netuid=netuid, snapshots=snapshots)
    return snapshots


def adaptive_bundle_at_anchor(
    netuid: int, snapshots: Sequence[AlphaPriceSnapshot], anchor_block: int
) -> RiskSubmissionBundle:
    """Build a forecast using only snapshots visible at ``anchor_block``."""
    past = tuple(snapshot for snapshot in snapshots if snapshot.block <= anchor_block)
    if not past:
        raise ValueError("anchor precedes the first snapshot")
    recent = tuple(
        snapshot
        for snapshot in past
        if snapshot.block > anchor_block - MAX_LOOKBACK_BLOCKS
    )
    series = (
        AlphaPriceSeries(
            source=f"backtest_as_of_{anchor_block}",
            netuid=netuid,
            snapshots=recent,
        )
        if recent
        else None
    )
    observation = past[-1].latest_pool_observation()
    return adaptive_risk_bundle(
        round_id=BACKTEST_ROUND_ID,
        netuids=(netuid,),
        latest_observation=lambda _netuid: observation,
        recent_price_series=lambda _netuid, _seconds: series,
    )


def _reference_value(
    output: RiskOutput, horizon: int, observation: AlphaPriceSnapshot
) -> int:
    if output in (RiskOutput.MAX_DRAWDOWN, RiskOutput.REALIZED_VOLATILITY):
        return BASELINE_BPS_VALUES[(output, horizon)]
    if output is RiskOutput.TWAP_PRICE:
        return observation.price_rao_per_alpha
    return observation.tao_reserve_rao


def _target_value(
    output: RiskOutput,
    future: tuple[AlphaPriceSnapshot, ...],
    anchor_block: int,
) -> int:
    if output is RiskOutput.MAX_DRAWDOWN:
        return max_drawdown_bps(future)
    if output is RiskOutput.REALIZED_VOLATILITY:
        return realized_volatility_bps(future)
    if output is RiskOutput.TWAP_PRICE:
        return twap_price_rao(future, window_start_block=anchor_block)
    return liquidity_depth_rao(future, window_start_block=anchor_block)


def _score_anchor(
    *,
    netuid: int,
    snapshots: tuple[AlphaPriceSnapshot, ...],
    anchor_block: int,
    outcome_lag_blocks: int,
    totals: Mapping[tuple[int, RiskOutput], ScoreTotals],
) -> None:
    bundle = adaptive_bundle_at_anchor(netuid, snapshots, anchor_block)
    forecasts = {
        (item.horizon_seconds, item.output): item for item in bundle.assets[0].outputs
    }
    current = next(s for s in reversed(snapshots) if s.block <= anchor_block)
    outcome_start = anchor_block + outcome_lag_blocks
    for horizon in RISK_HORIZONS:
        horizon_blocks = horizon_seconds_to_blocks(horizon)
        if (
            anchor_block < snapshots[0].block + horizon_blocks
            or outcome_start + horizon_blocks > snapshots[-1].block
        ):
            continue
        future = tuple(
            s
            for s in snapshots
            if outcome_start < s.block <= outcome_start + horizon_blocks
        )
        if should_void_realized_window(future, horizon_blocks=horizon_blocks):
            continue
        for output in RiskOutput:
            try:
                target = _target_value(output, future, outcome_start)
            except AlphaMarketDataError:
                continue
            forecast = forecasts[(horizon, output)]
            spec = RISK_SPECS_BY_OUTPUT[output]
            totals[(horizon, output)].add(
                adaptive=score_output(forecast.value, target, spec),
                reference=score_output(
                    _reference_value(output, horizon, current), target, spec
                ),
                used_history=ADAPTIVE_REASON_CODE in forecast.reason_codes,
            )


def build_report(
    rows_by_netuid: RecordedRows,
    *,
    anchor_step_blocks: int = DAY_BLOCKS,
    outcome_lag_blocks: int = DEFAULT_OUTCOME_LAG_BLOCKS,
    source: str = "recorded_fixture",
) -> dict[str, object]:
    """Score each as-of daily forecast against later resolved values."""
    if anchor_step_blocks <= 0:
        raise ValueError("anchor_step_blocks must be positive")
    if outcome_lag_blocks < 0:
        raise ValueError("outcome_lag_blocks must be non-negative")
    totals = {
        (horizon, output): ScoreTotals()
        for horizon in RISK_HORIZONS
        for output in RiskOutput
    }
    inputs: dict[str, dict[str, int]] = {}
    for netuid, rows in sorted(rows_by_netuid.items()):
        snapshots = _snapshots(netuid, rows)
        inputs[str(netuid)] = {
            "first_block": snapshots[0].block,
            "last_block": snapshots[-1].block,
            "snapshot_count": len(snapshots),
        }
        first_anchor = snapshots[0].block + horizon_seconds_to_blocks(RISK_HORIZONS[0])
        last_anchor = (
            snapshots[-1].block
            - horizon_seconds_to_blocks(RISK_HORIZONS[0])
            - outcome_lag_blocks
        )
        for anchor in range(first_anchor, last_anchor + 1, anchor_step_blocks):
            _score_anchor(
                netuid=netuid,
                snapshots=snapshots,
                anchor_block=anchor,
                outcome_lag_blocks=outcome_lag_blocks,
                totals=totals,
            )
    return {
        "source": source,
        "anchor_step_blocks": anchor_step_blocks,
        "outcome_lag_blocks": outcome_lag_blocks,
        "inputs": inputs,
        "scores": {
            str(horizon): {
                output.value: totals[(horizon, output)].report()
                for output in RiskOutput
            }
            for horizon in RISK_HORIZONS
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-json", type=Path, help="optional local netuid-to-snapshots JSON"
    )
    parser.add_argument("--anchor-step-blocks", type=int, default=DAY_BLOCKS)
    parser.add_argument(
        "--outcome-lag-blocks", type=int, default=DEFAULT_OUTCOME_LAG_BLOCKS
    )
    args = parser.parse_args()
    rows = (
        load_json_rows(args.input_json)
        if args.input_json is not None
        else RECORDED_ALPHA_MAINNET_ROWS
    )
    report = build_report(
        rows,
        anchor_step_blocks=args.anchor_step_blocks,
        outcome_lag_blocks=args.outcome_lag_blocks,
        source=str(args.input_json)
        if args.input_json is not None
        else "recorded_fixture",
    )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
