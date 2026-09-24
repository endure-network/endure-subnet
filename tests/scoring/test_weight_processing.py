"""Behavioral coverage for pure, replayable chain weight processing."""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal, Inexact, localcontext

import pytest

from endure.scoring.weight_processing import (
    U16_MAX,
    chain_weight_vector,
    convert_weights_and_uids_for_emit,
    emission_candidate,
    normalize_max_weight,
    normalize_scores,
    process_weights,
)


def D(value: str) -> Decimal:
    return Decimal(value)


class TestNormalizeMaxWeight:
    def test_zero_sum_returns_uniform(self) -> None:
        out = normalize_max_weight(
            [D("0"), D("0"), D("0"), D("0"), D("0")], limit=D("0.5")
        )
        assert out == [D("0.2")] * 5

    def test_len_times_limit_leq_one_returns_uniform(self) -> None:
        out = normalize_max_weight(
            [D("0.1"), D("0.2"), D("0.3"), D("0.4")],
            limit=D("0.25"),
        )
        assert out == [D("0.25")] * 4

    def test_already_compliant_is_just_normalized(self) -> None:
        out = normalize_max_weight(
            [D("1.0"), D("2.0"), D("3.0"), D("4.0")],
            limit=D("0.5"),
        )
        assert out == [D("0.1"), D("0.2"), D("0.3"), D("0.4")]
        assert sum(out) == D("1.0")

    def test_single_outlier_is_capped(self) -> None:
        out = normalize_max_weight(
            [D("1.0"), D("1.0"), D("1.0"), D("97.0")],
            limit=D("0.4"),
        )
        assert sum(out) == D("1")
        assert max(out) <= D("0.4")

    def test_preserves_ordering_of_non_capped_entries(self) -> None:
        out = normalize_max_weight(
            [D("1.0"), D("2.0"), D("3.0"), D("50.0")],
            limit=D("0.4"),
        )
        assert out[0] < out[1] < out[2]


class TestConvertWeightsAndUidsForEmit:
    def test_happy_path_u16_round_trip(self) -> None:
        out_uids, out_weights = convert_weights_and_uids_for_emit(
            [0, 1, 2],
            [D("0.5"), D("1.0"), D("0.25")],
        )
        assert out_uids == [0, 1, 2]
        assert out_weights == [32768, U16_MAX, 16384]

    def test_all_zero_weights_returns_empty_lists(self) -> None:
        out_uids, out_weights = convert_weights_and_uids_for_emit(
            [0, 1, 2],
            [D("0"), D("0"), D("0")],
        )
        assert out_uids == []
        assert out_weights == []

    def test_zero_weight_filter_drops_uid(self) -> None:
        out_uids, out_weights = convert_weights_and_uids_for_emit(
            [0, 1, 2],
            [D("0"), D("0.5"), D("1.0")],
        )
        assert 0 not in out_uids
        assert set(out_uids) == {1, 2}
        assert all(value > 0 for value in out_weights)

    def test_negative_weight_raises(self) -> None:
        with pytest.raises(ValueError, match="weight is negative"):
            convert_weights_and_uids_for_emit(
                [0, 1],
                [D("-0.1"), D("0.5")],
            )

    def test_negative_uid_raises(self) -> None:
        with pytest.raises(ValueError, match="uid is negative"):
            convert_weights_and_uids_for_emit(
                [-1, 0],
                [D("0.1"), D("0.5")],
            )

    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="same length"):
            convert_weights_and_uids_for_emit(
                [0, 1, 2],
                [D("0.1"), D("0.5")],
            )


class TestProcessWeights:
    def test_happy_path(self) -> None:
        out_uids, out_weights = process_weights(
            uids=list(range(8)),
            weights=[
                D("0"),
                D("0.1"),
                D("0.2"),
                D("0.3"),
                D("0.4"),
                D("0.05"),
                D("0.02"),
                D("0.01"),
            ],
            metagraph_size=8,
            min_allowed_weights=4,
            max_weight_limit=D("0.5"),
        )

        assert out_uids == [1, 2, 3, 4, 5, 6, 7]
        assert max(out_weights) <= D("0.5")
        assert sum(out_weights) == D("1")

    def test_no_positive_weights_returns_uniform_over_metagraph(self) -> None:
        out_uids, out_weights = process_weights(
            uids=[1, 5],
            weights=[D("-1"), D("0")],
            metagraph_size=8,
            min_allowed_weights=4,
            max_weight_limit=D("0.5"),
        )

        assert out_uids == list(range(8))
        assert out_weights == [D("0.125")] * 8

    def test_metagraph_smaller_than_min_allowed_returns_uniform(self) -> None:
        assert process_weights(
            uids=[0, 1],
            weights=[D("0.1"), D("0.9")],
            metagraph_size=2,
            min_allowed_weights=8,
            max_weight_limit=D("0.5"),
        ) == ([0, 1], [D("0.5"), D("0.5")])

    def test_sparse_reordered_uids_are_padded_by_uid_not_position(self) -> None:
        out_uids, out_weights = process_weights(
            uids=[5, 2],
            weights=[D("0.5"), D("0.5")],
            metagraph_size=8,
            min_allowed_weights=4,
            max_weight_limit=D("0.5"),
        )

        assert out_uids == list(range(8))
        assert sum(out_weights) == D("1")
        assert max(out_weights) <= D("0.5")
        assert out_weights[2] == out_weights[5]
        assert out_weights[2] > out_weights[0] > D("0")
        assert convert_weights_and_uids_for_emit(out_uids, out_weights) == (
            list(range(8)),
            [1, 1, 65535, 1, 1, 65535, 1, 1],
        )

    def test_sparse_reordered_uids_keep_their_weights(self) -> None:
        assert process_weights(
            uids=[7, 2, 5],
            weights=[D("3"), D("1"), D("6")],
            metagraph_size=8,
            min_allowed_weights=2,
            max_weight_limit=D("0.7"),
        ) == ([7, 2, 5], [D("0.3"), D("0.1"), D("0.6")])

    def test_exclude_quantile_drops_low_weights(self) -> None:
        out_uids, out_weights = process_weights(
            uids=list(range(8)),
            weights=[D(str(value)) for value in range(8)],
            metagraph_size=8,
            min_allowed_weights=4,
            max_weight_limit=D("0.5"),
            exclude_quantile=U16_MAX // 4,
        )

        assert out_uids == [3, 4, 5, 6, 7]
        assert sum(out_weights) == D("1")

    def test_exclusion_is_limited_by_minimum_allowed_weights(self) -> None:
        out_uids, out_weights = process_weights(
            uids=[4, 3, 2, 1, 0],
            weights=[D("1"), D("2"), D("3"), D("4"), D("5")],
            metagraph_size=5,
            min_allowed_weights=4,
            max_weight_limit=D("0.5"),
            exclude_quantile=U16_MAX,
        )

        assert out_uids == [3, 2, 1, 0]
        assert sum(out_weights) == D("1")

    def test_empty_metagraph_returns_empty_without_zero_division(self) -> None:
        assert process_weights(
            uids=[],
            weights=[],
            metagraph_size=0,
            min_allowed_weights=0,
            max_weight_limit=D("0.5"),
        ) == ([], [])

    @pytest.mark.parametrize("uid", [-1, 8])
    def test_invalid_uid_raises_before_uniform_or_floor_branch(self, uid: int) -> None:
        with pytest.raises(ValueError, match="outside metagraph"):
            process_weights(
                uids=[uid],
                weights=[D("1")],
                metagraph_size=8,
                min_allowed_weights=4,
                max_weight_limit=D("0.5"),
            )

    def test_mismatched_uid_weight_lengths_raise(self) -> None:
        with pytest.raises(ValueError, match="same length"):
            process_weights(
                uids=[0, 1, 2, 3],
                weights=[D("0.1"), D("0.2")],
                metagraph_size=4,
                min_allowed_weights=2,
                max_weight_limit=D("0.5"),
            )


@pytest.mark.parametrize(
    ("scores", "expected"),
    [
        ([D("-2"), D("1")], [D("0"), D("1")]),
        ([D("0"), D("-5")], [D("0"), D("0")]),
        ([D("1"), D("3")], [D("0.25"), D("0.75")]),
        ([], []),
    ],
)
def test_normalize_scores_clamps_before_normalizing(
    scores: list[Decimal], expected: list[Decimal]
) -> None:
    assert normalize_scores(scores) == expected


def test_emission_rounds_half_up_and_drops_only_rounded_zeros() -> None:
    assert convert_weights_and_uids_for_emit(
        [7, 3, 5, 1],
        [D("0.5"), D("1"), D("0.000001"), D("0.00001")],
    ) == ([7, 3, 1], [32768, 65535, 1])


@pytest.mark.parametrize("min_allowed", [2, 5, 9])
def test_replay_ignores_ambient_decimal_precision_rounding_and_traps(
    min_allowed: int,
) -> None:
    scores = [D("1"), D("2"), D("97"), D("0")]

    def replay() -> tuple[list[Decimal], list[int], list[Decimal], list[int]]:
        raw = normalize_scores(scores)
        uids, weights = process_weights(
            [7, 2, 5, 0],
            raw,
            metagraph_size=8,
            min_allowed_weights=min_allowed,
            max_weight_limit=D("0.4"),
        )
        uint_uids, uint_weights = convert_weights_and_uids_for_emit(uids, weights)
        assert uint_uids == uids
        return raw, uids, weights, uint_weights

    expected = replay()
    with localcontext() as context:
        context.clear_flags()
        context.prec = 6
        context.rounding = ROUND_DOWN
        context.traps[Inexact] = True
        assert replay() == expected
        assert normalize_max_weight([D("1"), D("2"), D("3")], D("0.5")) == [
            D("0.1666666666666666666666666667"),
            D("0.3333333333333333333333333333"),
            D("0.5"),
        ]
        assert context.prec == 6
        assert context.rounding == ROUND_DOWN
        assert context.traps[Inexact]
        assert not context.flags[Inexact]


class TestEmissionComposition:
    @pytest.mark.parametrize(
        "scores", [[], [D("0"), D("0")], [D("-1"), D("0")], [D("0E-28")]]
    )
    def test_nonpositive_scores_abstain_instead_of_uniform_weights(
        self, scores: list[D]
    ) -> None:
        assert emission_candidate(scores) is None

    @pytest.mark.parametrize(
        ("scores", "expected_uids", "expected_u16"),
        [
            ([D("0"), D("0"), D("1")], (2,), (65535,)),
            ([D("0"), D("0.6"), D("0.075"), D("-0.2")], (1, 2), (65535, 8192)),
        ],
    )
    def test_candidate_encodes_to_the_submitted_u16_vector(
        self,
        scores: list[D],
        expected_uids: tuple[int, ...],
        expected_u16: tuple[int, ...],
    ) -> None:
        raw = emission_candidate(scores)
        assert raw is not None

        vector = chain_weight_vector(
            raw,
            uids=list(range(len(scores))),
            metagraph_size=len(scores),
            min_allowed_weights=1,
            max_weight_limit=D("1"),
        )

        assert (vector.uint_uids, vector.uint_weights) == (expected_uids, expected_u16)
        assert sum(vector.processed_weights) == D("1")
