"""Deterministic Alpha Risk testnet calibration characterization.

This is evidence, not an optimizer: it reports how the shipped policy rewards
the reference baseline and a one-day-old persistence strategy against the
recorded fixture. It deliberately does not choose new consensus constants.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path

from endure.assessment.schemas.subnet_alpha_risk import (
    RISK_HORIZONS,
    RISK_SPECS_BY_OUTPUT,
    RiskOutput,
)
from endure.protocol.risk_miner import BASELINE_BPS_VALUES
from endure.scoring.assessment_orchestrator import score_output
from endure.scoring.market_data import AlphaPriceSnapshot
from endure.scoring.policy import (
    DEFAULT_PAYOUT_HALF_LIFE_ROUNDS,
    WEIGHT_SHARPENING_GAMMA,
)
from endure.scoring.recorded_fixtures.alpha_mainnet import RECORDED_ALPHA_MAINNET_ROWS
from endure.scoring.risk.observables import (
    horizon_seconds_to_blocks,
    liquidity_depth_rao,
    max_drawdown_bps,
    realized_volatility_bps,
    twap_price_rao,
)

CALIBRATION_ANCHOR_BLOCK = 7_410_000
ONE_DAY_BLOCKS = 24 * 60 * 60 // 12


def _fixture_hash() -> str:
    payload = json.dumps(
        RECORDED_ALPHA_MAINNET_ROWS,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _snapshot(netuid: int, row: tuple[int, str, int]) -> AlphaPriceSnapshot:
    block, price, reserve = row
    return AlphaPriceSnapshot(
        netuid=netuid,
        block=block,
        price_tao_per_alpha=Decimal(price),
        tao_reserve_rao=reserve,
    )


def _observation_at(netuid: int, block: int) -> AlphaPriceSnapshot:
    eligible = [row for row in RECORDED_ALPHA_MAINNET_ROWS[netuid] if row[0] <= block]
    if not eligible:
        raise ValueError(
            f"fixture has no observation for netuid={netuid} block={block}"
        )
    return _snapshot(netuid, eligible[-1])


def _window(netuid: int, horizon: int) -> tuple[AlphaPriceSnapshot, ...]:
    end_block = CALIBRATION_ANCHOR_BLOCK + horizon_seconds_to_blocks(horizon)
    return tuple(
        _snapshot(netuid, row)
        for row in RECORDED_ALPHA_MAINNET_ROWS[netuid]
        if CALIBRATION_ANCHOR_BLOCK < row[0] <= end_block
    )


def _targets(
    window: Sequence[AlphaPriceSnapshot],
) -> Mapping[RiskOutput, int]:
    return {
        RiskOutput.MAX_DRAWDOWN: max_drawdown_bps(window),
        RiskOutput.REALIZED_VOLATILITY: realized_volatility_bps(window),
        RiskOutput.TWAP_PRICE: twap_price_rao(
            window, window_start_block=CALIBRATION_ANCHOR_BLOCK
        ),
        RiskOutput.LIQUIDITY_DEPTH: liquidity_depth_rao(
            window, window_start_block=CALIBRATION_ANCHOR_BLOCK
        ),
    }


def _baseline_values(
    observation: AlphaPriceSnapshot, horizon: int
) -> Mapping[RiskOutput, int]:
    return {
        RiskOutput.MAX_DRAWDOWN: BASELINE_BPS_VALUES[
            (RiskOutput.MAX_DRAWDOWN, horizon)
        ],
        RiskOutput.REALIZED_VOLATILITY: BASELINE_BPS_VALUES[
            (RiskOutput.REALIZED_VOLATILITY, horizon)
        ],
        RiskOutput.TWAP_PRICE: observation.price_rao_per_alpha,
        RiskOutput.LIQUIDITY_DEPTH: observation.tao_reserve_rao,
    }


def _weight_share(score: Decimal, clone_count: int) -> Decimal:
    clone_power = Decimal(clone_count) * score**WEIGHT_SHARPENING_GAMMA
    return clone_power / (Decimal(1) + clone_power)


def build_report() -> dict[str, object]:
    coordinate_rows: list[dict[str, object]] = []
    scores: dict[str, list[Decimal]] = {
        "perfect": [],
        "reference_baseline": [],
        "previous_round_persistence": [],
    }
    for netuid in sorted(RECORDED_ALPHA_MAINNET_ROWS):
        current_observation = _observation_at(netuid, CALIBRATION_ANCHOR_BLOCK)
        previous_observation = _observation_at(
            netuid, CALIBRATION_ANCHOR_BLOCK - ONE_DAY_BLOCKS
        )
        for horizon in RISK_HORIZONS:
            target_values = _targets(_window(netuid, horizon))
            strategy_values = {
                "perfect": target_values,
                "reference_baseline": _baseline_values(current_observation, horizon),
                "previous_round_persistence": _baseline_values(
                    previous_observation, horizon
                ),
            }
            for output in RiskOutput:
                output_scores = {
                    strategy: score_output(
                        values[output],
                        target_values[output],
                        RISK_SPECS_BY_OUTPUT[output],
                    )
                    for strategy, values in strategy_values.items()
                }
                for strategy, value in output_scores.items():
                    scores[strategy].append(value)
                coordinate_rows.append(
                    {
                        "horizon_seconds": horizon,
                        "netuid": netuid,
                        "output": output.value,
                        "scores": {
                            strategy: str(value)
                            for strategy, value in sorted(output_scores.items())
                        },
                        "target": target_values[output],
                    }
                )
    mean_scores = {
        strategy: sum(values, Decimal(0)) / Decimal(len(values))
        for strategy, values in scores.items()
    }
    return {
        "anchor_block": CALIBRATION_ANCHOR_BLOCK,
        "coordinate_scores": coordinate_rows,
        "fixture_sha256": _fixture_hash(),
        "payout_half_life_rounds": DEFAULT_PAYOUT_HALF_LIFE_ROUNDS,
        "strategy_mean_scores": {
            strategy: str(value) for strategy, value in sorted(mean_scores.items())
        },
        "weight_scenarios_against_one_perfect_miner": {
            strategy: {
                str(clones): str(_weight_share(mean_scores[strategy], clones))
                for clones in (1, 3, 5)
            }
            for strategy in ("reference_baseline", "previous_round_persistence")
        },
        "weight_sharpening_gamma": WEIGHT_SHARPENING_GAMMA,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rendered = json.dumps(build_report(), indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
