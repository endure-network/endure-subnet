"""Pure, release-pinned cold-start selection and bootstrap safety checks."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from endure.protocol.consensus_policy import (
    MAINNET_GENESIS_HASH,
    SN30_BOOTSTRAP_HOTKEY,
    SN30_BOOTSTRAP_NETUID,
    SN30_BOOTSTRAP_UID,
)
from endure.scoring.weight_processing import U16_MAX


class BootstrapPolicyError(ValueError):
    """Bootstrap cannot safely use the supplied chain state."""


@dataclass(frozen=True, slots=True)
class EmissionCandidate:
    mode: Literal["bootstrap", "scored", "abstain"]
    weights: tuple[Decimal, ...]


def select_emission_candidate(
    scores: Sequence[Decimal],
    *,
    bootstrap_enabled: bool,
    has_positive_history: bool,
) -> EmissionCandidate:
    """Select weights without changing scores or durable graduation history."""
    if any(score > 0 for score in scores):
        return EmissionCandidate("scored", tuple(scores))
    if not bootstrap_enabled or has_positive_history:
        return EmissionCandidate("abstain", ())
    if len(scores) <= SN30_BOOTSTRAP_UID:
        raise BootstrapPolicyError("Bootstrap recipient is outside the score vector")
    return EmissionCandidate(
        "bootstrap",
        tuple(
            Decimal(1) if uid == SN30_BOOTSTRAP_UID else Decimal(0)
            for uid in range(len(scores))
        ),
    )


def validate_bootstrap_recipient(
    *,
    chain_identity: str,
    netuid: int,
    hotkeys: Sequence[str],
    owner_hotkey: str | None,
) -> None:
    """Require the pinned mainnet subnet, registered recipient, and owner."""
    if chain_identity != MAINNET_GENESIS_HASH or netuid != SN30_BOOTSTRAP_NETUID:
        raise BootstrapPolicyError("Bootstrap requires the pinned mainnet SN30 chain")
    if len(hotkeys) <= SN30_BOOTSTRAP_UID:
        raise BootstrapPolicyError("Bootstrap recipient is not registered")
    if hotkeys[SN30_BOOTSTRAP_UID] != SN30_BOOTSTRAP_HOTKEY:
        raise BootstrapPolicyError("Bootstrap recipient hotkey has changed")
    if owner_hotkey != SN30_BOOTSTRAP_HOTKEY:
        raise BootstrapPolicyError("Subnet owner does not match bootstrap recipient")


def bootstrap_submission_due(  # noqa: PLR0913 — explicit chain snapshot and identity
    *,
    validator_uid: int,
    validator_hotkey: str,
    hotkeys: Sequence[str],
    validator_permits: Sequence[bool],
    last_updates: Sequence[int],
    block: int,
    weights_rate_limit: int,
) -> bool:
    """Check current validator identity, permit, and inclusive rate boundary."""
    if validator_uid < 0 or validator_uid >= len(hotkeys):
        raise BootstrapPolicyError("Validator UID is not registered")
    if not validator_hotkey or hotkeys[validator_uid] != validator_hotkey:
        raise BootstrapPolicyError("Validator UID does not match its hotkey")
    if validator_uid >= len(validator_permits) or validator_uid >= len(last_updates):
        raise BootstrapPolicyError("Validator metadata is missing")
    last_update = last_updates[validator_uid]
    if block < 0 or weights_rate_limit < 0 or last_update < 0 or last_update > block:
        raise BootstrapPolicyError("Incoherent bootstrap block or rate data")
    return (
        validator_permits[validator_uid] and block - last_update >= weights_rate_limit
    )


def validate_bootstrap_vector(
    *,
    uids: Sequence[int],
    weights: Sequence[int],
    min_allowed_weights: int | None,
    max_weight_limit: Decimal | None,
) -> None:
    """Reject chain constraints or processing that alter the owner allocation."""
    if (
        min_allowed_weights != 1
        or max_weight_limit is None
        or not max_weight_limit.is_finite()
        or max_weight_limit != Decimal(1)
    ):
        raise BootstrapPolicyError("Bootstrap requires chain minimum 1 and maximum 1")
    if (
        len(uids) != 1
        or len(weights) != 1
        or uids[0] != SN30_BOOTSTRAP_UID
        or weights[0] != U16_MAX
    ):
        raise BootstrapPolicyError("Bootstrap requires the exact recipient u16 vector")
