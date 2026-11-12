"""Blended score across horizons (risk scope spec §EMA and earned weights)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from endure.assessment.coordinates import AssessmentCoordinate, AssessmentEmaState
from endure.assessment.schemas.subnet_alpha_risk import RISK_HORIZONS
from endure.scoring.policy import (
    DEFAULT_PAYOUT_HALF_LIFE_ROUNDS,
    LONG_HORIZON_HOLD_BACK,
)
from endure.scoring.weights import blended_score, ema_maturity, ema_update

SHORT, LONG = RISK_HORIZONS
HALF_LIFE = DEFAULT_PAYOUT_HALF_LIFE_ROUNDS
GOOD = Decimal("0.9")
MATURE = 200
TOLERANCE = Decimal("1e-20")


def _state(
    horizon: int,
    ema: Decimal,
    rounds: int,
    netuid: int = 1,
    output: str = "twap_price",
) -> AssessmentEmaState:
    return AssessmentEmaState(
        miner_hotkey="hk",
        coordinate=AssessmentCoordinate.subnet_asset(
            netuid=netuid, horizon_seconds=horizon, output=output
        ),
        ema=ema,
        resolved_rounds=rounds,
    )


def _record(horizon: int, scores: list[Decimal]) -> AssessmentEmaState:
    ema = Decimal(0)
    for score in scores:
        ema = ema_update(ema, score, half_life_rounds=HALF_LIFE)
    return _state(horizon, ema, len(scores))


def _blend(*states: AssessmentEmaState) -> Decimal:
    return blended_score(states, horizons=RISK_HORIZONS, half_life_rounds=HALF_LIFE)


def test_maturity_is_half_after_one_half_life() -> None:
    assert ema_maturity(HALF_LIFE, half_life_rounds=HALF_LIFE) == Decimal("0.5")
    assert ema_maturity(0, half_life_rounds=HALF_LIFE) == 0


def test_maturity_rejects_a_non_positive_half_life() -> None:
    with pytest.raises(ValueError, match="half_life_rounds"):
        ema_maturity(1, half_life_rounds=0)


def test_one_horizon_is_the_plain_mean() -> None:
    states = [
        _state(SHORT, Decimal("0.2"), 1),
        _state(SHORT, Decimal("0.6"), 1, netuid=2),
    ]
    blend = blended_score(states, horizons=(SHORT,), half_life_rounds=HALF_LIFE)
    assert blend == Decimal("0.4")


def test_no_long_record_keeps_the_mean_less_the_hold_back() -> None:
    short = _record(SHORT, [GOOD] * 12)
    expected = short.ema * (1 - LONG_HORIZON_HOLD_BACK)
    assert abs(_blend(short) - expected) < TOLERANCE


def test_every_long_round_at_equal_accuracy_raises_the_blend() -> None:
    short = _record(SHORT, [GOOD] * MATURE)
    blends = [
        _blend(short, *([_record(LONG, [GOOD] * rounds)] if rounds else []))
        for rounds in range(25)
    ]
    assert all(later > earlier for earlier, later in zip(blends, blends[1:]))


def test_a_mature_record_scores_its_accuracy() -> None:
    blend = _blend(_record(SHORT, [GOOD] * MATURE), _record(LONG, [GOOD] * MATURE))
    assert abs(blend - GOOD) < Decimal("1e-9")


def test_poor_long_accuracy_lowers_the_blend() -> None:
    short = _record(SHORT, [GOOD] * MATURE)
    good = _blend(short, _record(LONG, [GOOD] * 20))
    poor = _blend(short, _record(LONG, [Decimal("0.1")] * 20))
    assert poor < good


def test_missed_long_rounds_lower_the_blend() -> None:
    short = _record(SHORT, [GOOD] * MATURE)
    present = _blend(short, _record(LONG, [GOOD] * 4))
    absent = _blend(short, _record(LONG, [GOOD, *([Decimal(0)] * 3)]))
    assert absent < present


def test_a_pair_without_a_long_row_is_held_back_alone() -> None:
    mature = [_state(SHORT, GOOD, MATURE), _state(LONG, GOOD, MATURE)]
    held = _state(SHORT, GOOD, MATURE, netuid=2)
    expected = (GOOD + GOOD * (1 - LONG_HORIZON_HOLD_BACK)) / 2
    assert abs(_blend(*mature, held) - expected) < Decimal("1e-9")


def test_a_long_row_without_a_short_row_counts_half() -> None:
    blend = _blend(_state(LONG, Decimal("0.5"), MATURE))
    assert abs(blend - Decimal("0.25")) < Decimal("1e-9")


def test_the_first_short_round_after_archival_raises_the_blend() -> None:
    long = _record(LONG, [GOOD] * 100)
    assert _blend(_record(SHORT, [Decimal(1)]), long) > _blend(long)


@pytest.mark.parametrize("horizon", RISK_HORIZONS)
@pytest.mark.parametrize(
    ("short_rounds", "long_rounds"),
    [
        ([], [GOOD] * 100),
        ([Decimal(0)], [GOOD] * 100),
        ([Decimal(0)] * 30, [GOOD] * 5),
        ([GOOD], [GOOD] * 100),
        ([GOOD] * 30, []),
        ([GOOD] * 30, [Decimal(0)] * 5),
        ([GOOD] * 30, [GOOD]),
        ([GOOD] * MATURE, [GOOD] * MATURE),
    ],
)
def test_a_zero_observation_never_raises_the_blend(
    horizon: int, short_rounds: list[Decimal], long_rounds: list[Decimal]
) -> None:
    def blend(short: list[Decimal], long: list[Decimal]) -> Decimal:
        rows = [(SHORT, short), (LONG, long)]
        return _blend(*(_record(h, scores) for h, scores in rows if scores))

    zero = [Decimal(0)]
    after = (
        blend(short_rounds + zero, long_rounds)
        if horizon == SHORT
        else blend(short_rounds, long_rounds + zero)
    )
    assert after <= blend(short_rounds, long_rounds)


def test_equal_maturity_keeps_the_ratio_of_the_plain_means() -> None:
    def rows(short: Decimal, long: Decimal) -> tuple[AssessmentEmaState, ...]:
        return _state(SHORT, short, MATURE), _state(LONG, long, 8)

    better = _blend(*rows(Decimal("0.8"), Decimal("0.3")))
    worse = _blend(*rows(Decimal("0.4"), Decimal("0.15")))
    assert abs(better / worse - 2) < TOLERANCE


def test_row_order_does_not_change_the_blend() -> None:
    states = [
        _state(horizon, Decimal(1) / Decimal(3 + netuid), MATURE, netuid=netuid)
        for horizon in RISK_HORIZONS
        for netuid in range(1, 16)
    ]
    assert _blend(*states) == _blend(*reversed(states))


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ([(SHORT, 1, "0.6", 3)], "0.48"),
        ([(SHORT, 1, "0.6", 100)], "0.48"),
        ([(SHORT, 1, "0.9", 40), (LONG, 1, "0.3", 5)], "0.72"),
        ([(SHORT, 1, "0.8", 40), (LONG, 1, "0.6", 10)], "0.76"),
        ([(LONG, 1, "0.25", 5)], "0.15"),
        (
            [
                (SHORT, 1, "0.9", 40),
                (LONG, 1, "0.3", 5),
                (SHORT, 2, "0.8", 2),
                (LONG, 2, "0.6", 10),
                (SHORT, 3, "0.6", 30),
                (LONG, 4, "0.25", 5),
            ],
            "0.5275",
        ),
    ],
)
def test_literal_blends(rows: list[tuple[int, int, str, int]], expected: str) -> None:
    states = [
        _state(horizon, Decimal(ema), rounds, netuid=netuid)
        for horizon, netuid, ema, rounds in rows
    ]
    assert abs(_blend(*states) - Decimal(expected)) < TOLERANCE


def test_pairs_are_keyed_by_output() -> None:
    states = [
        _state(SHORT, Decimal("0.9"), 40),
        _state(LONG, Decimal("0.3"), 5),
        _state(SHORT, Decimal("0.6"), 3, output="max_drawdown"),
    ]
    assert abs(_blend(*states) - Decimal("0.60")) < TOLERANCE


def test_no_state_scores_zero() -> None:
    assert _blend() == 0
