"""Tests for endure.utils.misc.

Covers ttl_cache decorator, _ttl_hash_gen generator, and ttl_get_block
helper. These are pure-Python utilities with no chain dependency; we
patch time.time and use a MagicMock subtensor instead of spinning up
bittensor mocks.
"""

from __future__ import annotations

from math import floor
from unittest.mock import MagicMock, patch

import pytest

from endure.utils import misc
from endure.utils.misc import _ttl_hash_gen, ttl_cache, ttl_get_block


class TestTtlCache:
    def test_caches_result_across_calls(self) -> None:
        call_count = {"n": 0}

        @ttl_cache(maxsize=4, ttl=60)
        def compute(x: int) -> int:
            call_count["n"] += 1
            return x * 2

        assert compute(3) == 6
        assert compute(3) == 6
        assert call_count["n"] == 1

        assert compute(4) == 8
        assert call_count["n"] == 2

    def test_respects_maxsize_eviction(self) -> None:
        call_count = {"n": 0}

        @ttl_cache(maxsize=2, ttl=60)
        def compute(x: int) -> int:
            call_count["n"] += 1
            return x

        compute(1)
        compute(2)
        compute(3)
        assert call_count["n"] == 3
        compute(1)
        assert call_count["n"] == 4

    def test_ttl_eviction_by_patching_time(self) -> None:
        call_count = {"n": 0}

        with patch.object(misc, "time") as fake_time:
            fake_time.time.return_value = 1000.0

            @ttl_cache(maxsize=4, ttl=10)
            def compute(x: int) -> int:
                call_count["n"] += 1
                return x

            compute(7)
            compute(7)
            assert call_count["n"] == 1

            fake_time.time.return_value = 1015.0
            compute(7)
            assert call_count["n"] == 2

    def test_ttl_non_positive_sets_large_default(self) -> None:
        call_count = {"n": 0}

        @ttl_cache()
        def compute(x: int) -> int:
            call_count["n"] += 1
            return x + 1

        compute(9)
        compute(9)
        assert call_count["n"] == 1

    def test_preserves_wrapped_function_metadata(self) -> None:
        @ttl_cache(maxsize=1, ttl=5)
        def my_func(x: int) -> int:
            """docstring."""
            return x

        assert my_func.__name__ == "my_func"
        assert my_func.__doc__ == "docstring."


class TestTtlHashGen:
    def test_yields_monotonic_floor_values(self) -> None:
        with patch.object(misc, "time") as fake_time:
            # Generator body is lazy: the FIRST next() consumes both
            # start_time = time.time() (100.0) AND the first yield's
            # time.time() (100.0 -> floor(0/10)=0). Each subsequent
            # next() consumes one additional time.time().
            fake_time.time.side_effect = [100.0, 100.0, 115.0, 130.0]
            gen = _ttl_hash_gen(10)
            h0 = next(gen)
            h1 = next(gen)
            h2 = next(gen)
        assert h0 == 0
        assert h1 == floor(15 / 10)
        assert h2 == floor(30 / 10)
        assert h0 <= h1 <= h2

    def test_interval_boundary(self) -> None:
        # Same generator-laziness rule as above: start_time=0.0 is captured
        # by the first time.time() on the first next(). floor(29.999/10)==2
        # but floor(30.0/10)==3 asserts strict-floor semantics at the boundary.
        with patch.object(misc, "time") as fake_time:
            fake_time.time.side_effect = [0.0, 20.0, 29.999, 30.0]
            gen = _ttl_hash_gen(10)
            vals = [next(gen), next(gen), next(gen)]
        assert vals == [2, 2, 3]


class TestTtlGetBlock:
    def test_delegates_to_subtensor_and_caches(self) -> None:
        fake_self = MagicMock()
        fake_self.subtensor.get_current_block.return_value = 42

        # First call hits the underlying method.
        result = ttl_get_block(fake_self)
        assert result == 42

        # Second call within TTL: hash value hasn't advanced, so the lru_cache
        # on (ttl_hash, self) must serve from cache and the underlying call
        # count must still be 1.
        assert ttl_get_block(fake_self) == 42
        assert fake_self.subtensor.get_current_block.call_count == 1

    def test_different_self_instances_are_separate_cache_entries(self) -> None:
        a, b = MagicMock(), MagicMock()
        a.subtensor.get_current_block.return_value = 100
        b.subtensor.get_current_block.return_value = 200

        assert ttl_get_block(a) == 100
        assert ttl_get_block(b) == 200
        assert a.subtensor.get_current_block.call_count == 1
        assert b.subtensor.get_current_block.call_count == 1


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
