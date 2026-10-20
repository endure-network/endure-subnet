"""Behavioral coverage for pure, replayable chain weight processing."""

from __future__ import annotations

import itertools
import random
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, Inexact, localcontext

import pytest

from endure.scoring.context import TR_CONTEXT
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


class TestGroupedRounding:
    """A group of UIDs, such as one identical-forecast pool, rounds as one entry."""

    def test_a_group_gets_what_one_entry_with_its_total_would(self) -> None:
        # Each copy scales to 0.65535 units; fifty of them to 32.7675.
        uids = list(range(51))
        weights = [D("1")] + [D("0.00001")] * 50

        _, apart = convert_weights_and_uids_for_emit(uids, weights)
        grouped = convert_weights_and_uids_for_emit(
            uids, weights, groups=[range(1, 51)]
        )

        assert sum(apart) == 65535 + 50
        assert convert_weights_and_uids_for_emit([0, 1], [D("1"), D("0.0005")]) == (
            [0, 1],
            [65535, 33],
        )
        assert grouped == ([0, *range(1, 34)], [65535] + [1] * 33)

    def test_members_keep_their_floor_and_the_rest_goes_by_remainder(self) -> None:
        # 1.96605, 1.3107 and 0.65535 units: floors 1, 1 and 0, total 3.9321.
        assert convert_weights_and_uids_for_emit(
            [4, 5, 6, 0],
            [D("0.00003"), D("0.00002"), D("0.00001"), D("1")],
            groups=[(4, 5, 6)],
        ) == ([4, 5, 6, 0], [2, 1, 1, 65535])

    def test_a_group_of_small_members_keeps_the_unit_its_total_rounds_to(
        self,
    ) -> None:
        # 0.327675, 0.39321 and 0.458745 units: each alone rounds to zero.
        uids = [0, 1, 2, 3]
        weights = [D("1"), D("0.000005"), D("0.000006"), D("0.000007")]

        assert convert_weights_and_uids_for_emit(uids, weights) == ([0], [65535])
        assert convert_weights_and_uids_for_emit(uids, weights, groups=[(1, 2, 3)]) == (
            [0, 3],
            [65535, 1],
        )

    @pytest.mark.parametrize("first", [9, 4, 7])
    def test_equal_remainders_go_in_the_group_order(self, first: int) -> None:
        order = (first, *(uid for uid in (9, 4, 7) if uid != first))

        assert convert_weights_and_uids_for_emit(
            [9, 4, 7, 0], [D("0.000006")] * 3 + [D("1")], groups=[order]
        ) == ([first, 0], [1, 65535])

    def test_a_group_total_of_one_half_rounds_up(self) -> None:
        assert convert_weights_and_uids_for_emit(
            [0, 1, 2], [D(65535), D("0.25"), D("0.25")], groups=[(1, 2)]
        ) == ([0, 1], [65535, 1])

    def test_a_groups_total_does_not_depend_on_its_order(self) -> None:
        # About 1.5 units in all, within 1e-27 of the half: 28-digit sums in
        # member order fell on either side of it, depending on the order. The
        # encoder sums the remainders smallest first, so every order agrees.
        weights = [
            D("1"),
            D("0.000007629510948348210879682612344"),
            D("0.000007629510948348210879682612344"),
            D("0.000007629510948348210879682612321"),
            D("1.525902189669642175936522469E-32"),
        ]
        totals = {
            sum(
                convert_weights_and_uids_for_emit(
                    list(range(5)), weights, groups=[order]
                )[1][1:]
            )
            for order in itertools.permutations(range(1, 5))
        }

        assert len(totals) == 1

    def test_small_remainders_are_summed_first(self) -> None:
        # Pins the encoder's 28-digit sum, smallest first. Exactly, six
        # remainders of 8.4e-30 leave 0.4999...9 under the half; added first,
        # they make the last 28-digit addition round to 0.5, while adding them
        # after it drops each one. Every validator sums alike, so all agree.
        weights = [D(65535), D("0.4999999999999999999999999999")]
        weights += [D("8.4E-30")] * 6

        assert convert_weights_and_uids_for_emit(
            list(range(8)), weights, groups=[range(1, 8)]
        ) == ([0, 1], [65535, 1])

    def test_the_largest_entry_stays_the_u16_maximum(self) -> None:
        assert convert_weights_and_uids_for_emit(
            [0, 1, 2], [D("1"), D("0.5"), D("0.5")], groups=[(0, 1, 2)]
        ) == ([0, 1, 2], [65535, 32768, 32767])

    def test_uids_outside_the_vector_are_skipped(self) -> None:
        assert convert_weights_and_uids_for_emit(
            [0, 1], [D("1"), D("0.000006")], groups=[(1, 2, 3)]
        ) == ([0], [65535])

    def test_ungrouped_and_lone_uids_round_half_up_as_before(self) -> None:
        rng = random.Random(2044)  # noqa: S311 — a seeded case generator
        for _ in range(200):
            size = rng.randint(1, 64)
            uids = rng.sample(range(256), size)
            weights = [D(rng.randint(0, 10**6)) / D(10**6) for _ in range(size)]
            weights[rng.randrange(size)] = D(rng.randint(1, 10**6)) / D(10**6)
            with localcontext(TR_CONTEXT):
                top = max(weights)
                units = [
                    int((weight / top * U16_MAX).quantize(D("1"), ROUND_HALF_UP))
                    for weight in weights
                ]
            expected = (
                [uid for uid, unit in zip(uids, units, strict=True) if unit],
                [unit for unit in units if unit],
            )

            assert convert_weights_and_uids_for_emit(uids, weights) == expected
            assert (
                convert_weights_and_uids_for_emit(
                    uids, weights, groups=[(uid,) for uid in uids]
                )
                == expected
            )

    @pytest.mark.parametrize(
        ("uids", "groups"),
        [
            ([1, 2, 3], [(1, 2), (2, 3)]),
            ([1, 2, 3], [(1, 2, 1)]),
            ([1, 1, 2], [(1, 2)]),
        ],
    )
    def test_groups_need_distinct_uids_in_one_group_each(
        self, uids: list[int], groups: list[tuple[int, ...]]
    ) -> None:
        with pytest.raises(ValueError):
            convert_weights_and_uids_for_emit(uids, [D("1")] * len(uids), groups=groups)

    def test_grouped_rounding_ignores_the_ambient_decimal_context(self) -> None:
        uids = [9, 4, 7, 0]
        weights = [D("0.000006"), D("0.000007"), D("0.000005"), D("1")]
        expected = convert_weights_and_uids_for_emit(uids, weights, groups=[(9, 4, 7)])
        with localcontext() as context:
            context.prec = 6
            context.rounding = ROUND_DOWN
            context.traps[Inexact] = True
            assert (
                convert_weights_and_uids_for_emit(uids, weights, groups=[(9, 4, 7)])
                == expected
            )
        assert expected == ([4, 0], [1, 65535])


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

    def test_the_vector_rounds_each_group_as_one_entry(self) -> None:
        raw = emission_candidate([D("1")] + [D("0.00001")] * 50)
        assert raw is not None

        vector = chain_weight_vector(
            raw,
            uids=list(range(51)),
            metagraph_size=51,
            min_allowed_weights=1,
            max_weight_limit=D("1"),
            groups=[range(1, 51)],
        )

        assert vector.uint_weights == (65535,) + (1,) * 33
