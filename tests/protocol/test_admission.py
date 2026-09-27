"""Miner admission for commit/reveal submissions."""

from __future__ import annotations

from decimal import Decimal

from endure.protocol.admission import miner_admission
from endure.protocol.consensus_policy import MIN_MINER_STAKE

REGISTERED = ("hk-poor", "hk-rich")
STAKE_WEIGHTS = (Decimal("9.99"), Decimal("10"))


def _admission(hotkey: str | None, floor: Decimal) -> tuple[bool, str]:
    return miner_admission(
        hotkey,
        registered_hotkeys=REGISTERED,
        stake_weight=STAKE_WEIGHTS.__getitem__,
        min_stake=floor,
    )


def test_missing_hotkey_is_refused() -> None:
    assert _admission(None, MIN_MINER_STAKE) == (True, "Missing dendrite or hotkey")


def test_unregistered_hotkey_is_refused() -> None:
    assert _admission("hk-stranger", MIN_MINER_STAKE) == (True, "Unrecognized hotkey")


def test_canonical_floor_admits_every_registered_hotkey() -> None:
    for hotkey in REGISTERED:
        assert _admission(hotkey, MIN_MINER_STAKE) == (False, "Hotkey recognized")


def test_positive_floor_is_inclusive_on_stake_weight() -> None:
    assert _admission("hk-poor", Decimal("10")) == (True, "Insufficient stake")
    assert _admission("hk-rich", Decimal("10")) == (False, "Hotkey recognized")
