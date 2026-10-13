from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from endure.scoring.weight_processing import U16_MAX
from scripts.sybil_economics.__main__ import DEFAULT_INPUT, ROOT, build_report
from scripts.sybil_economics.models import Experiment, Policy, PublicSnapshot
from scripts.sybil_economics.report import (
    cost_rows,
    entry_rows,
    experiments,
    live_observations,
    registration_cost,
)
from scripts.sybil_economics.simulation import (
    allocate,
    chain_result,
    consensus_result,
    coordinated_scenario,
    lifecycle_score,
    perturb,
    scenario,
    score_values,
)


@pytest.fixture(scope="module")
def snapshot() -> PublicSnapshot:
    return PublicSnapshot.model_validate_json(DEFAULT_INPUT.read_bytes())


@pytest.fixture(scope="module")
def fixture(snapshot: PublicSnapshot) -> Experiment:
    return experiments(ROOT, snapshot)[0]


def test_replication_obeys_operator_aggregate_formula(fixture: Experiment) -> None:
    forecasts = scenario(fixture, 5, "exact")
    earned, _ = allocate(forecasts, fixture, Policy("current"))
    subject = sum((earned[row.name] for row in forecasts[1:]), Decimal(0))
    score = score_values(fixture.cells, fixture.reference)
    expected = 5 * score**3 / (1 + 5 * score**3)
    assert abs(subject - expected) < Decimal("1e-25")
    assert subject > Decimal("0.7347")


def test_exact_pool_is_invariant_only_for_exact_forecasts(fixture: Experiment) -> None:
    policy = Policy("exact", pooling="exact")
    one, _ = allocate(scenario(fixture, 1, "exact"), fixture, policy)
    copies = scenario(fixture, 5, "exact")
    pooled, _ = allocate(copies, fixture, policy)
    assert abs(
        sum((pooled[row.name] for row in copies[1:]), Decimal(0)) - one["subject-0"]
    ) < Decimal("1e-25")
    near = scenario(fixture, 5, "near_scaled")
    escaped, _ = allocate(near, fixture, policy)
    assert sum((escaped[row.name] for row in near[1:]), Decimal(0)) > Decimal("0.73")


def test_correct_independent_agreement_is_penalized(fixture: Experiment) -> None:
    honest = scenario(fixture, 5, "correct_agreement", independent=True)
    current, _ = allocate(honest, fixture, Policy("current"))
    pooled, _ = allocate(honest, fixture, Policy("exact", pooling="exact"))
    assert all(row.score == 1 for row in honest[1:])
    assert all(pooled[row.name] < current[row.name] for row in honest[1:])
    assert pooled["subject-0"] / current["subject-0"] < Decimal("0.72")


def test_ownership_labels_do_not_change_any_policy(fixture: Experiment) -> None:
    copies = scenario(fixture, 5, "near_scaled")
    independent = tuple(
        replace(row, operator=f"independent-{index}")
        for index, row in enumerate(copies)
    )
    policy = Policy("near", pooling="near", tolerance=Decimal("0.5"))
    assert allocate(copies, fixture, policy) == allocate(independent, fixture, policy)
    assert allocate(copies, fixture, policy) == allocate(
        tuple(reversed(copies)), fixture, policy
    )


def test_perturbation_cannot_read_future_targets(fixture: Experiment) -> None:
    unknown = replace(
        fixture,
        cells=tuple(cell.model_copy(update={"target": None}) for cell in fixture.cells),
    )
    assert perturb(fixture, 3, True) == perturb(unknown, 3, True)


def test_grace_band_distortion_defeats_forecast_pooling() -> None:
    base, one = coordinated_scenario(1, True)
    attacked, copies = coordinated_scenario(3, True)
    _, before = allocate(one, base, Policy("current"))
    assert consensus_result(one, base, before)["diagnostic_30d_tiers"]["8"] == "C"
    assert all(row.score == 1 for row in copies)
    for policy in (
        Policy("current"),
        Policy("exact", pooling="exact"),
        Policy("tight", pooling="near", tolerance=Decimal("0.01")),
        Policy("wide", pooling="near", tolerance=Decimal("0.5")),
    ):
        _, after = allocate(copies, attacked, policy)
        assert (
            consensus_result(copies, attacked, after)["diagnostic_30d_tiers"]["8"]
            == "B"
        )


def test_horizons_mature_separately_from_zero_prior(fixture: Experiment) -> None:
    assert lifecycle_score(fixture, 5) == 0
    assert lifecycle_score(fixture, 6) > 0
    assert lifecycle_score(fixture, 30) > lifecycle_score(fixture, 31)
    assert lifecycle_score(fixture, 60) > lifecycle_score(fixture, 31)


def test_burn_encoding_is_not_candidate_reward(
    fixture: Experiment, snapshot: PublicSnapshot
) -> None:
    forecasts = scenario(fixture, 5, "exact")
    earned, _ = allocate(forecasts, fixture, Policy("current"))
    encoded = chain_result(
        forecasts, earned, snapshot.chain.burn_bps, snapshot.chain.max_uids
    )
    assert encoded["uint_pairs"][0] == (0, U16_MAX)
    assert Decimal(encoded["encoded_subject_total_vote_share"]) < Decimal("0.02")
    assert Decimal(encoded["encoded_subject_miner_pool_share"]) > Decimal("0.73")
    fully_burned = chain_result(forecasts, earned, 10000, snapshot.chain.max_uids)
    assert fully_burned["encoded_subject_miner_pool_share"] is None


def test_perfect_new_entrant_can_round_to_zero(snapshot: PublicSnapshot) -> None:
    live = experiments(ROOT, snapshot)[1]
    rows = entry_rows(snapshot, score_values(live.cells, live.reference))
    first = next(
        row
        for row in rows
        if row["constant_raw_accuracy"] == "1" and row["resolved_5d_rounds"] == 1
    )
    assert Decimal(first["candidate_miner_share"]) > 0
    assert first["encoded_entry_u16"] == 0


def test_registration_floor_does_not_bound_fast_price(snapshot: PublicSnapshot) -> None:
    fast = registration_cost(snapshot.chain, 20, 1)
    paced = registration_cost(snapshot.chain, 20, snapshot.chain.burn_half_life_blocks)
    assert fast > paced
    assert paced >= Decimal(20 * snapshot.chain.min_registration_burn_rao) / Decimal(
        10**9
    )


def test_observed_chain_constraints_are_required(tmp_path: Path) -> None:
    raw = json.loads(DEFAULT_INPUT.read_bytes())
    raw["chain"]["max_weight_limit_u16"] = 100
    altered = tmp_path / "changed-chain.json"
    altered.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="observed chain limits"):
        build_report(altered)


def test_existing_operator_buys_only_additional_registrations(
    fixture: Experiment, snapshot: PublicSnapshot
) -> None:
    one_new = next(
        row for row in cost_rows(fixture, snapshot.chain) if row["identities"] == 2
    )
    assert Decimal(one_new["incremental_fast_registration_tao"]) == Decimal(
        snapshot.chain.registration_burn_rao
    ) / Decimal(10**9)
    assert one_new["spaced_registration_duration_days"] == "0"


def test_reported_alpha_uses_epoch_mapping(snapshot: PublicSnapshot) -> None:
    changed = list(snapshot.chain.registered_hotkeys)
    forecasts = snapshot.rounds[-1].forecasts
    largest_vector = Counter(row.values for row in forecasts).most_common(1)[0][0]
    subject = next(row.hotkey for row in forecasts if row.values == largest_vector)
    index = changed.index(subject)
    changed[index] = "hypothetical-later-registration"
    altered = snapshot.model_copy(
        update={
            "chain": snapshot.chain.model_copy(
                update={"registered_hotkeys": tuple(changed)}
            )
        }
    )
    original_groups = live_observations(snapshot)["rounds"][-1]["largest_groups"]
    altered_groups = live_observations(altered)["rounds"][-1]["largest_groups"]
    assert [
        group["chain_reported_unwithheld_miner_alpha"] for group in original_groups
    ] == [group["chain_reported_unwithheld_miner_alpha"] for group in altered_groups]


def test_unknown_runtime_identity_is_not_replaced_with_repository_policy(
    tmp_path: Path,
) -> None:
    raw = json.loads(DEFAULT_INPUT.read_bytes())
    raw["health"]["content_revision"] = "unknown"
    altered = tmp_path / "unknown-runtime.json"
    altered.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="runtime policy identity differs"):
        build_report(altered)
