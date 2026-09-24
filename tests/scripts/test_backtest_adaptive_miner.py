from __future__ import annotations

import json
from decimal import Decimal

import pytest

from endure.assessment.schemas.subnet_alpha_risk import (
    HORIZON_5D_SECONDS,
    HORIZON_30D_SECONDS,
)
from endure.scoring.market_data import AlphaPriceSnapshot
from scripts.backtest_adaptive_miner import (
    DAY_BLOCKS,
    adaptive_bundle_at_anchor,
    build_report,
    load_json_rows,
)


def test_recorded_fixture_reports_as_of_five_day_scores_and_no_thirty_day_samples() -> (
    None
):
    from endure.scoring.recorded_fixtures.alpha_mainnet import (
        RECORDED_ALPHA_MAINNET_ROWS,
    )

    report = build_report(RECORDED_ALPHA_MAINNET_ROWS)
    five_day = report["scores"][str(HORIZON_5D_SECONDS)]
    thirty_day = report["scores"][str(HORIZON_30D_SECONDS)]

    assert set(report["inputs"]) == {"8", "44"}
    assert {item["eligible_samples"] for item in five_day.values()} == {50}
    assert {item["eligible_samples"] for item in thirty_day.values()} == {0}
    assert {item["adaptive_mean_score"] for item in thirty_day.values()} == {None}
    assert Decimal(five_day["twap_price"]["adaptive_mean_score"]) < Decimal(
        five_day["twap_price"]["reference_mean_score"]
    )
    assert Decimal(five_day["liquidity_depth"]["adaptive_mean_score"]) < Decimal(
        five_day["liquidity_depth"]["reference_mean_score"]
    )


def test_forecast_at_anchor_does_not_see_future_snapshots() -> None:
    anchor = 5 * DAY_BLOCKS
    past = tuple(
        AlphaPriceSnapshot(
            netuid=8,
            block=index * 600,
            price_tao_per_alpha=Decimal("0.04"),
            tao_reserve_rao=10_000_000_000,
        )
        for index in range(anchor // 600 + 1)
    )
    future = (
        AlphaPriceSnapshot(
            netuid=8,
            block=anchor + 600,
            price_tao_per_alpha=Decimal("0.001"),
            tao_reserve_rao=1_000_000,
        ),
    )

    without_future = adaptive_bundle_at_anchor(8, past, anchor)
    with_future = adaptive_bundle_at_anchor(8, past + future, anchor)

    assert with_future == without_future


def test_longer_dataset_has_thirty_day_eligible_samples() -> None:
    rows = {
        8: tuple(
            (index * 600, "0.04", 10_000_000_000)
            for index in range(65 * DAY_BLOCKS // 600 + 1)
        )
    }

    report = build_report(rows, anchor_step_blocks=5 * DAY_BLOCKS)
    thirty_day = report["scores"][str(HORIZON_30D_SECONDS)]

    assert {item["eligible_samples"] for item in thirty_day.values()} == {1}
    assert {item["adaptive_history_samples"] for item in thirty_day.values()} == {1}


def test_local_json_prices_must_be_decimal_strings(tmp_path) -> None:  # noqa: ANN001
    dataset = tmp_path / "snapshots.json"
    dataset.write_text(
        json.dumps({"8": [[600, "0.04", 10_000_000_000]]}), encoding="utf-8"
    )
    assert load_json_rows(dataset) == {8: ((600, "0.04", 10_000_000_000),)}

    dataset.write_text(
        json.dumps({"8": [[600, 0.04, 10_000_000_000]]}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="price a string"):
        load_json_rows(dataset)
