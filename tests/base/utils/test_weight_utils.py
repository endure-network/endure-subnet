"""Tests for endure.base.utils.weight_utils."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from endure.base.utils.weight_utils import (
    U16_MAX,
    convert_weights_and_uids_for_emit,
    normalize_max_weight,
    process_weights_for_netuid,
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
        assert out_weights[1] == U16_MAX
        assert abs(out_weights[0] - U16_MAX // 2) <= 1
        assert abs(out_weights[2] - U16_MAX // 4) <= 1

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


class TestProcessWeightsForNetuid:
    @staticmethod
    def _subtensor(min_allowed: int, max_limit: str) -> MagicMock:
        st = MagicMock()
        st.min_allowed_weights.return_value = min_allowed
        st.max_weight_limit.return_value = max_limit
        return st

    @staticmethod
    def _metagraph(n: int) -> MagicMock:
        mg = MagicMock()
        mg.n = n
        return mg

    def test_happy_path(self) -> None:
        st = self._subtensor(min_allowed=4, max_limit="0.5")
        mg = self._metagraph(n=8)

        out_uids, out_weights = process_weights_for_netuid(
            uids=list(range(8)),
            weights=[
                D("0.0"),
                D("0.1"),
                D("0.2"),
                D("0.3"),
                D("0.4"),
                D("0.05"),
                D("0.02"),
                D("0.01"),
            ],
            netuid=1,
            subtensor=st,
            metagraph=mg,
        )

        assert len(out_uids) == len(out_weights)
        assert 0 not in out_uids
        assert max(out_weights) <= D("0.5")
        assert sum(out_weights) == D("1")

    def test_no_non_zero_weights_returns_uniform_over_metagraph(self) -> None:
        st = self._subtensor(min_allowed=4, max_limit="0.5")
        mg = self._metagraph(n=8)

        out_uids, out_weights = process_weights_for_netuid(
            uids=list(range(8)),
            weights=[D("0")] * 8,
            netuid=1,
            subtensor=st,
            metagraph=mg,
        )

        assert out_uids == list(range(8))
        assert out_weights == [D("0.125")] * 8

    def test_metagraph_smaller_than_min_allowed_returns_uniform(self) -> None:
        st = self._subtensor(min_allowed=8, max_limit="0.5")
        mg = self._metagraph(n=2)

        out_uids, out_weights = process_weights_for_netuid(
            uids=[0, 1],
            weights=[D("0.5"), D("0.5")],
            netuid=1,
            subtensor=st,
            metagraph=mg,
        )

        assert out_uids == [0, 1]
        assert out_weights == [D("0.5"), D("0.5")]

    def test_non_zero_less_than_min_allowed_uses_floor_branch(self) -> None:
        st = self._subtensor(min_allowed=4, max_limit="0.5")
        mg = self._metagraph(n=8)

        out_uids, out_weights = process_weights_for_netuid(
            uids=list(range(8)),
            weights=[
                D("0.0"),
                D("0.0"),
                D("0.5"),
                D("0.0"),
                D("0.0"),
                D("0.5"),
                D("0.0"),
                D("0.0"),
            ],
            netuid=1,
            subtensor=st,
            metagraph=mg,
        )

        assert len(out_uids) == 8
        assert sum(out_weights) == D("1")
        assert out_weights[2] > out_weights[0]
        assert out_weights[5] > out_weights[0]

    def test_exclude_quantile_drops_low_weights(self) -> None:
        st = self._subtensor(min_allowed=4, max_limit="0.5")
        mg = self._metagraph(n=8)

        out_uids, out_weights = process_weights_for_netuid(
            uids=list(range(8)),
            weights=[
                D("0.0"),
                D("0.1"),
                D("0.2"),
                D("0.3"),
                D("0.4"),
                D("0.5"),
                D("0.6"),
                D("0.7"),
            ],
            netuid=1,
            subtensor=st,
            metagraph=mg,
            exclude_quantile=U16_MAX // 4,
        )

        assert len(out_uids) == len(out_weights)
        assert 1 not in out_uids
        assert sum(out_weights) == D("1")

    def test_metagraph_is_fetched_when_none(self) -> None:
        st = self._subtensor(min_allowed=2, max_limit="0.5")
        fetched_mg = self._metagraph(n=4)
        st.metagraph.return_value = fetched_mg

        out_uids, out_weights = process_weights_for_netuid(
            uids=[0, 1, 2, 3],
            weights=[D("0.1"), D("0.2"), D("0.3"), D("0.4")],
            netuid=7,
            subtensor=st,
            metagraph=None,
        )
        st.metagraph.assert_called_once_with(7)
        assert len(out_uids) == len(out_weights)

    def test_empty_metagraph_returns_empty_without_zero_division(self) -> None:
        st = self._subtensor(min_allowed=0, max_limit="0.5")
        mg = self._metagraph(n=0)

        out_uids, out_weights = process_weights_for_netuid(
            uids=[],
            weights=[],
            netuid=1,
            subtensor=st,
            metagraph=mg,
        )

        assert out_uids == []
        assert out_weights == []

    def test_negative_uid_raises_before_uniform_branch(self) -> None:
        st = self._subtensor(min_allowed=8, max_limit="0.5")
        mg = self._metagraph(n=4)

        with pytest.raises(ValueError, match="outside metagraph"):
            process_weights_for_netuid(
                uids=[-1],
                weights=[D("1")],
                netuid=1,
                subtensor=st,
                metagraph=mg,
            )

    def test_out_of_bounds_uid_raises_before_floor_branch(self) -> None:
        st = self._subtensor(min_allowed=4, max_limit="0.5")
        mg = self._metagraph(n=8)

        with pytest.raises(ValueError, match="outside metagraph"):
            process_weights_for_netuid(
                uids=[8],
                weights=[D("1")],
                netuid=1,
                subtensor=st,
                metagraph=mg,
            )

    def test_mismatched_uid_weight_lengths_raise(self) -> None:
        st = self._subtensor(min_allowed=2, max_limit="0.5")
        mg = self._metagraph(n=4)

        with pytest.raises(ValueError, match="same length"):
            process_weights_for_netuid(
                uids=[0, 1, 2, 3],
                weights=[D("0.1"), D("0.2")],
                netuid=1,
                subtensor=st,
                metagraph=mg,
            )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
