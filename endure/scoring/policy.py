"""Shared consensus-critical Alpha Risk scoring defaults."""

from typing import Final

# Experimental Alpha Risk testnet starting value. The public calibration report
# characterizes its behavior; it is not evidence of Sybil resistance or economic
# readiness, and stable/mainnet activation requires a separate soak decision.
WEIGHT_SHARPENING_GAMMA: Final = 3

# Experimental Alpha Risk testnet payout memory. Keep lockstep with the public
# calibration artifact and change only through a versioned protocol update.
DEFAULT_PAYOUT_HALF_LIFE_ROUNDS: Final = 5
