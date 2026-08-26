"""Shared deterministic Decimal context (risk scope spec §Scoring)."""

from decimal import ROUND_HALF_EVEN, Context

TR_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)
