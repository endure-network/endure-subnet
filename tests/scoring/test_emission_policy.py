"""Behavioral coverage for cold-start allocation and permanent graduation."""

from __future__ import annotations

from decimal import Decimal

import pytest

from endure.protocol.consensus_policy import (
    MAINNET_GENESIS_HASH,
    SN30_BOOTSTRAP_HOTKEY,
    SN30_BOOTSTRAP_NETUID,
    SN30_BOOTSTRAP_UID,
)
from endure.scoring.emission_policy import (
    BootstrapPolicyError,
    bootstrap_submission_due,
    select_emission_candidate,
    validate_bootstrap_recipient,
    validate_bootstrap_vector,
)


def test_cold_start_allocates_only_to_owner_without_creating_scores() -> None:
    scores = [Decimal(0)] * 200

    candidate = select_emission_candidate(
        scores, bootstrap_enabled=True, has_positive_history=False
    )

    assert candidate.mode == "bootstrap"
    assert candidate.weights == (Decimal(0),) * 176 + (Decimal(1),) + (Decimal(0),) * 23
    assert scores == [Decimal(0)] * 200


@pytest.mark.parametrize("bootstrap_enabled", [False, True])
@pytest.mark.parametrize("has_positive_history", [False, True])
def test_positive_scores_take_precedence_and_preserve_values(
    bootstrap_enabled: bool, has_positive_history: bool
) -> None:
    scores = [Decimal("-0.2"), Decimal("0.15"), Decimal("0.35")]

    candidate = select_emission_candidate(
        scores,
        bootstrap_enabled=bootstrap_enabled,
        has_positive_history=has_positive_history,
    )
    scores[1] = Decimal(0)

    assert candidate.mode == "scored"
    assert candidate.weights == (Decimal("-0.2"), Decimal("0.15"), Decimal("0.35"))


@pytest.mark.parametrize("scores", [[], [Decimal(0)], [Decimal("-1"), Decimal(0)]])
@pytest.mark.parametrize(
    ("bootstrap_enabled", "has_positive_history"),
    [(False, False), (False, True), (True, True)],
)
def test_disabled_or_graduated_nonpositive_scores_abstain(
    scores: list[Decimal], bootstrap_enabled: bool, has_positive_history: bool
) -> None:
    candidate = select_emission_candidate(
        scores,
        bootstrap_enabled=bootstrap_enabled,
        has_positive_history=has_positive_history,
    )

    assert candidate.mode == "abstain"
    assert candidate.weights == ()


@pytest.mark.parametrize("size", [0, 176])
def test_bootstrap_requires_recipient_within_vector(size: int) -> None:
    with pytest.raises(BootstrapPolicyError):
        select_emission_candidate(
            [Decimal(0)] * size,
            bootstrap_enabled=True,
            has_positive_history=False,
        )

    candidate = select_emission_candidate(
        [Decimal(0)] * 177, bootstrap_enabled=True, has_positive_history=False
    )
    assert candidate.mode == "bootstrap"
    assert candidate.weights == (Decimal(0),) * 176 + (Decimal(1),)


@pytest.mark.parametrize(
    ("chain_identity", "netuid", "recipient", "owner", "size"),
    [
        (
            "testnet-genesis",
            SN30_BOOTSTRAP_NETUID,
            SN30_BOOTSTRAP_HOTKEY,
            SN30_BOOTSTRAP_HOTKEY,
            177,
        ),
        (
            MAINNET_GENESIS_HASH.upper(),
            SN30_BOOTSTRAP_NETUID,
            SN30_BOOTSTRAP_HOTKEY,
            SN30_BOOTSTRAP_HOTKEY,
            177,
        ),
        (MAINNET_GENESIS_HASH, 31, SN30_BOOTSTRAP_HOTKEY, SN30_BOOTSTRAP_HOTKEY, 177),
        (
            MAINNET_GENESIS_HASH,
            SN30_BOOTSTRAP_NETUID,
            "replacement",
            SN30_BOOTSTRAP_HOTKEY,
            177,
        ),
        (
            MAINNET_GENESIS_HASH,
            SN30_BOOTSTRAP_NETUID,
            SN30_BOOTSTRAP_HOTKEY,
            "replacement",
            177,
        ),
        (MAINNET_GENESIS_HASH, SN30_BOOTSTRAP_NETUID, SN30_BOOTSTRAP_HOTKEY, None, 177),
        (
            MAINNET_GENESIS_HASH,
            SN30_BOOTSTRAP_NETUID,
            SN30_BOOTSTRAP_HOTKEY,
            SN30_BOOTSTRAP_HOTKEY,
            176,
        ),
    ],
)
def test_recipient_validation_rejects_changed_chain_registration_or_owner(
    chain_identity: str, netuid: int, recipient: str, owner: str | None, size: int
) -> None:
    hotkeys = ["other"] * 177
    hotkeys[SN30_BOOTSTRAP_UID] = SN30_BOOTSTRAP_HOTKEY
    validate_bootstrap_recipient(
        chain_identity=MAINNET_GENESIS_HASH,
        netuid=SN30_BOOTSTRAP_NETUID,
        hotkeys=hotkeys,
        owner_hotkey=SN30_BOOTSTRAP_HOTKEY,
    )

    hotkeys[SN30_BOOTSTRAP_UID] = recipient
    with pytest.raises(BootstrapPolicyError):
        validate_bootstrap_recipient(
            chain_identity=chain_identity,
            netuid=netuid,
            hotkeys=hotkeys[:size],
            owner_hotkey=owner,
        )


@pytest.mark.parametrize(
    ("permit", "last_update", "block", "rate_limit", "expected"),
    [
        (True, 100, 279, 180, False),
        (True, 100, 280, 180, True),
        (True, 100, 281, 180, True),
        (False, 100, 280, 180, False),
        (True, 100, 100, 0, True),
    ],
)
def test_submission_obeys_permit_and_inclusive_rate_boundary(
    permit: bool, last_update: int, block: int, rate_limit: int, expected: bool
) -> None:
    assert (
        bootstrap_submission_due(
            validator_uid=1,
            validator_hotkey="validator",
            hotkeys=["other", "validator"],
            validator_permits=[False, permit],
            last_updates=[0, last_update],
            block=block,
            weights_rate_limit=rate_limit,
        )
        is expected
    )


@pytest.mark.parametrize(
    ("uid", "hotkey", "permits", "updates", "block", "rate_limit"),
    [
        (-1, "validator", [False, True], [0, 100], 280, 180),
        (2, "validator", [False, True], [0, 100], 280, 180),
        (0, "validator", [False, True], [0, 100], 280, 180),
        (1, "", [False, True], [0, 100], 280, 180),
        (1, "validator", [False], [0, 100], 280, 180),
        (1, "validator", [False, True], [0], 280, 180),
        (1, "validator", [False, True], [0, 100], -1, 180),
        (1, "validator", [False, True], [0, 100], 280, -1),
        (1, "validator", [False, True], [0, -1], 280, 180),
        (1, "validator", [False, False], [0, 281], 280, 180),
    ],
)
def test_submission_rejects_identity_or_incoherent_metadata(
    uid: int,
    hotkey: str,
    permits: list[bool],
    updates: list[int],
    block: int,
    rate_limit: int,
) -> None:
    with pytest.raises(BootstrapPolicyError):
        bootstrap_submission_due(
            validator_uid=uid,
            validator_hotkey=hotkey,
            hotkeys=["other", "validator"],
            validator_permits=permits,
            last_updates=updates,
            block=block,
            weights_rate_limit=rate_limit,
        )


@pytest.mark.parametrize(
    ("uids", "weights", "minimum", "maximum"),
    [
        ([], [], 1, Decimal(1)),
        ([175], [65535], 1, Decimal(1)),
        ([176], [65534], 1, Decimal(1)),
        ([176, 177], [65535, 0], 1, Decimal(1)),
        ([176, 176], [65535, 65535], 1, Decimal(1)),
        ([176], [65535, 0], 1, Decimal(1)),
        ([176], [65535], None, Decimal(1)),
        ([176], [65535], 0, Decimal(1)),
        ([176], [65535], 2, Decimal(1)),
        ([176], [65535], 1, None),
        ([176], [65535], 1, Decimal("0.5")),
        ([176], [65535], 1, Decimal(2)),
        ([176], [65535], 1, Decimal("NaN")),
        ([176], [65535], 1, Decimal("sNaN")),
        ([176], [65535], 1, Decimal("Infinity")),
    ],
)
def test_vector_validation_refuses_padding_redirection_or_incompatible_constraints(
    uids: list[int],
    weights: list[int],
    minimum: int | None,
    maximum: Decimal | None,
) -> None:
    validate_bootstrap_vector(
        uids=[176],
        weights=[65535],
        min_allowed_weights=1,
        max_weight_limit=Decimal(1),
    )

    with pytest.raises(BootstrapPolicyError):
        validate_bootstrap_vector(
            uids=uids,
            weights=weights,
            min_allowed_weights=minimum,
            max_weight_limit=maximum,
        )
