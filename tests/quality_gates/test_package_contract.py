from __future__ import annotations

import pytest

import endure
from endure import _encode_spec_version


def test_package_version_matches_fixed_width_encoder() -> None:
    assert endure.__version__ == "0.1.0"
    assert endure.__spec_version__ == _encode_spec_version(endure.__version__)


def test_fixed_width_encoder_is_collision_free_and_monotonic() -> None:
    assert _encode_spec_version("0.1.10") != _encode_spec_version("0.2.0")
    assert (
        _encode_spec_version("0.1.0")
        < _encode_spec_version("0.1.1")
        < _encode_spec_version("0.2.0")
        < _encode_spec_version("1.0.0")
    )


def test_fixed_width_encoder_rejects_out_of_range_fields() -> None:
    with pytest.raises(ValueError):
        _encode_spec_version("0.0.1000")
