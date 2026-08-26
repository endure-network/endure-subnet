"""Shared consensus-critical scoring defaults (spec §8)."""

from typing import Final

# Weight sharpening: weights ∝ score**gamma. Tuned 2026-06-11 by replay —
# linear (γ=1) left the emission gradient too flat (always_zero ≈ persistence)
# and the early-warner premium at 1.12x; γ=3 passes the >=1.5x gate.
WEIGHT_SHARPENING_GAMMA: Final = 3

# Payout memory: shortest half-life where better strategies stay on top in
# replay. Replay-selected 2026-06-11 (calibration report); must match the
# design §8 constants summary — guarded by tests/quality_gates.
DEFAULT_PAYOUT_HALF_LIFE_ROUNDS: Final = 5
