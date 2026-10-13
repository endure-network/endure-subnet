"""Generate reproducible evidence, keeping live observations and experiments distinct."""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from time import process_time_ns

from pydantic import BaseModel

from endure.assessment.schemas.subnet_alpha_risk import RiskOutput
from endure.protocol.risk_miner import BASELINE_BPS_VALUES
from endure.scoring.emission_policy import owner_burn_weights
from endure.scoring.policy import DEFAULT_PAYOUT_HALF_LIFE_ROUNDS
from endure.scoring.recorded_fixtures.alpha_mainnet import RECORDED_ALPHA_MAINNET_ROWS
from endure.scoring.weight_processing import U16_MAX, chain_weight_vector
from endure.scoring.weights import ema_update, normalize_weights
from scripts.sybil_economics.models import (
    Cell,
    ChainEvidence,
    Experiment,
    Policy,
    PublicSnapshot,
)
from scripts.sybil_economics.simulation import (
    ONE,
    ZERO,
    allocate,
    chain_result,
    consensus_result,
    coordinated_scenario,
    lifecycle_score,
    scenario,
    score_values,
)

RAO = Decimal(10**9)
ONE_DAY_BLOCKS = 7200
COUNTS = (1, 2, 3, 5, 10, 20, 107, 254)
SMALL_COUNT_LIMIT = 20
STRATEGIES = ("exact", "near_unit", "near_scaled", "stale_public", "correct_agreement")
LIFECYCLE_DAYS = (0, 5, 6, 10, 20, 30, 31, 40, 60)
HOLDING_DAYS = (14, 30)


class HistoricalCell(BaseModel):
    netuid: int
    horizon_seconds: int
    output: RiskOutput
    target: int


class HistoricalInput(BaseModel):
    anchor_block: int
    fixture_sha256: str
    coordinate_scores: tuple[HistoricalCell, ...]


def _fixture_values(cells: tuple[Cell, ...], block: int) -> tuple[int, ...]:
    values: list[int] = []
    for cell in cells:
        if (cell.output, cell.horizon_seconds) in BASELINE_BPS_VALUES:
            values.append(BASELINE_BPS_VALUES[(cell.output, cell.horizon_seconds)])
            continue
        observation = max(
            (
                row
                for row in RECORDED_ALPHA_MAINNET_ROWS[cell.netuid]
                if row[0] <= block
            ),
            key=lambda row: row[0],
        )
        value = (
            int(Decimal(observation[1]) * RAO)
            if cell.output == RiskOutput.TWAP_PRICE
            else observation[2]
        )
        values.append(value)
    return tuple(values)


def experiments(root: Path, snapshot: PublicSnapshot) -> tuple[Experiment, ...]:
    historical = HistoricalInput.model_validate_json(
        (root / "docs/calibration/alpha-risk-v1-rc1.json").read_text()
    )
    cells = tuple(Cell(**cell.model_dump()) for cell in historical.coordinate_scores)
    fixture = Experiment(
        "historical_static_oracle_diagnostic",
        cells,
        _fixture_values(cells, historical.anchor_block),
        _fixture_values(cells, historical.anchor_block - ONE_DAY_BLOCKS),
    )
    current = next(row for row in snapshot.rounds if row.round_id == "2026-10-05")
    previous = next(row for row in snapshot.rounds if row.round_id == "2026-10-03")
    if str(previous.meta["publication_available_at"]) >= str(
        current.meta["commit_close_at"]
    ):
        raise ValueError(
            "stale forecast's scheduled embargo lift is after the commitment cutoff"
        )
    resolved = tuple(
        index for index, cell in enumerate(current.cells) if cell.target is not None
    )
    reference = Counter(row.values for row in current.forecasts).most_common(1)[0][0]
    stale_row = min(previous.forecasts, key=lambda row: row.hotkey)
    prior_values = {
        (cell.netuid, cell.horizon_seconds, cell.output): value
        for cell, value in zip(previous.cells, stale_row.values, strict=True)
    }
    live_cells = tuple(current.cells[index] for index in resolved)
    live = Experiment(
        "live_2026-10-05_resolved_5d_oracle_diagnostic",
        live_cells,
        tuple(reference[index] for index in resolved),
        tuple(
            prior_values[(cell.netuid, cell.horizon_seconds, cell.output)]
            for cell in live_cells
        ),
    )
    return fixture, live


def policies(experiment: Experiment) -> tuple[Policy, ...]:
    return (
        Policy("current"),
        Policy("exact_pool", pooling="exact"),
        Policy("near_pool_0.01_grace", pooling="near", tolerance=Decimal("0.01")),
        Policy("near_pool_0.50_grace", pooling="near", tolerance=Decimal("0.50")),
        Policy(
            "public_baseline_excess",
            baseline_score=score_values(experiment.cells, experiment.stale),
        ),
        Policy("gamma_12_diagnostic", gamma=12),
    )


def _scenario_row(
    experiment: Experiment,
    count: int,
    strategy: str,
    policy: Policy,
    chain: ChainEvidence,
) -> dict[str, object]:
    forecasts = scenario(
        experiment, count, strategy, independent=strategy == "correct_agreement"
    )
    earned, publication = allocate(forecasts, experiment, policy)
    original = scenario(
        experiment, 1, strategy, independent=strategy == "correct_agreement"
    )
    original_earned, _ = allocate(original, experiment, policy)
    original_share = original_earned["subject-0"]
    subject_share = sum((earned[row.name] for row in forecasts[1:]), ZERO)
    current, _ = allocate(forecasts, experiment, Policy("current"))
    current_subject = sum((current[row.name] for row in forecasts[1:]), ZERO)
    return {
        "experiment": experiment.name,
        "strategy": strategy,
        "copies": count,
        "policy": policy.name,
        "subject_mean_raw_score": str(
            sum((row.score for row in forecasts[1:]), ZERO) / Decimal(count)
        ),
        "subject_share": str(subject_share),
        "per_identity_share": [str(earned[row.name]) for row in forecasts[1:]],
        "amplification_vs_one": str(subject_share / original_share)
        if original_share
        else None,
        "competitor_share": str(earned["competitor"]),
        "subject_loss_vs_current_policy": str(ONE - subject_share / current_subject)
        if current_subject
        else None,
        "consensus": consensus_result(forecasts, experiment, publication),
        "chain": chain_result(forecasts, earned, chain.burn_bps, chain.max_uids),
        "control_semantics": "independent operators with identical observable histories/forecasts receive these same allocations; operator labels never enter the policy",
    }


def exposure_rows(
    experiment: Experiment, chain: ChainEvidence
) -> list[dict[str, object]]:
    rows = []
    for strategy in STRATEGIES:
        for count in COUNTS:
            if count > SMALL_COUNT_LIMIT and strategy != "exact":
                continue
            for policy in policies(experiment):
                if count > SMALL_COUNT_LIMIT and policy.pooling == "near":
                    continue
                rows.append(_scenario_row(experiment, count, strategy, policy, chain))
    return rows


def lifecycle_rows(experiment: Experiment) -> list[dict[str, object]]:
    mature = score_values(experiment.cells, experiment.reference)
    rows = []
    for day in LIFECYCLE_DAYS:
        new_score = lifecycle_score(experiment, day)
        for copies in (1, 5, 20):
            mass = mature**3 + Decimal(copies - 1) * new_score**3
            rows.append(
                {
                    "day": day,
                    "total_operator_identities": copies,
                    "new_clone_score": str(new_score),
                    "operator_share_existing_mature_plus_new_clones": str(
                        mass / (ONE + mass)
                    ),
                    "unproven_clone_publication_mass": str(
                        Decimal(copies - 1) * max(new_score, Decimal("0.01"))
                    ),
                }
            )
    return rows


def coordinated_rows() -> list[dict[str, object]]:
    rows = []
    for perturbed in (False, True):
        for copies in (1, 3):
            experiment, forecasts = coordinated_scenario(copies, perturbed)
            for policy in policies(experiment):
                earned, publication = allocate(forecasts, experiment, policy)
                rows.append(
                    {
                        "copies": copies,
                        "perturbed": perturbed,
                        "policy": policy.name,
                        "all_raw_scores": [str(row.score) for row in forecasts],
                        "subject_share": str(
                            sum(
                                (
                                    earned[row.name]
                                    for row in forecasts
                                    if row.operator == "subject-operator"
                                ),
                                ZERO,
                            )
                        ),
                        "consensus": consensus_result(
                            forecasts, experiment, publication
                        ),
                    }
                )
    return rows


def entry_rows(
    snapshot: PublicSnapshot, reference_accuracy: Decimal
) -> list[dict[str, object]]:
    """Freeze other histories; replace a zero-score UID in a labelled counterfactual."""
    chain = snapshot.chain
    scores = {row.hotkey: row.blended_score for row in snapshot.leaderboard}
    blends = {
        hotkey: scores.get(hotkey, ZERO)
        for uid, hotkey in enumerate(chain.registered_hotkeys)
        if chain.coldkeys[uid] != chain.owner_coldkey
    }
    placeholder = next(
        uid
        for uid, hotkey in enumerate(chain.registered_hotkeys)
        if uid != chain.owner_uid
        and not chain.validator_permits[uid]
        and blends.get(hotkey, ZERO) == ZERO
    )
    rows = []
    for accuracy in (reference_accuracy, ONE):
        for resolved_rounds in (0, 1, 2, 5, 10, 20):
            ema = ZERO
            for _ in range(resolved_rounds):
                ema = ema_update(
                    ema, accuracy, half_life_rounds=DEFAULT_PAYOUT_HALF_LIFE_ROUNDS
                )
            augmented = dict(blends)
            augmented[chain.registered_hotkeys[placeholder]] = ema
            earned = normalize_weights(augmented)
            vector = [earned.get(hotkey, ZERO) for hotkey in chain.registered_hotkeys]
            raw = owner_burn_weights(
                vector, owner_uid=chain.owner_uid, burn_bps=chain.burn_bps
            )
            if raw is None:
                raise ValueError("observed scored cohort unexpectedly empty")
            encoded = chain_weight_vector(
                raw,
                uids=range(len(vector)),
                metagraph_size=len(vector),
                min_allowed_weights=chain.min_allowed_weights,
                max_weight_limit=Decimal(chain.max_weight_limit_u16) / Decimal(U16_MAX),
            )
            entry = dict(zip(encoded.uint_uids, encoded.uint_weights, strict=True)).get(
                placeholder, 0
            )
            rows.append(
                {
                    "resolved_5d_rounds": resolved_rounds,
                    "constant_raw_accuracy": str(accuracy),
                    "entry_ema": str(ema),
                    "candidate_miner_share": str(
                        earned[chain.registered_hotkeys[placeholder]]
                    ),
                    "encoded_entry_u16": entry,
                    "uid_placeholder": placeholder,
                    "limits": "hypothetical replacement of a current zero-score non-validator UID; actual pruning recipient, successful registration, validator synchronization, and Yuma outcome are not assumed",
                }
            )
    return rows


def registration_cost(chain: ChainEvidence, count: int, spacing: int) -> Decimal:
    """Current-parameter price sensitivity; not a historical payment reconstruction."""
    multiplier = Decimal(chain.burn_increase_u64f64_bits) / Decimal(2**64)
    decay = Decimal(2) ** (-Decimal(spacing) / Decimal(chain.burn_half_life_blocks))
    price = Decimal(chain.registration_burn_rao)
    cost = ZERO
    for _ in range(count):
        cost += price.to_integral_value(rounding=ROUND_CEILING)
        price = max(
            Decimal(chain.min_registration_burn_rao),
            min(Decimal(chain.max_registration_burn_rao), price * multiplier * decay),
        )
    return cost / RAO


def cost_rows(experiment: Experiment, chain: ChainEvidence) -> list[dict[str, object]]:
    mature = score_values(experiment.cells, experiment.reference)
    initial = mature**3 / (ONE + mature**3)
    rows = []
    for count in COUNTS:
        if count == 1:
            continue
        for days in HOLDING_DAYS:
            share_days = ZERO
            for day in range(1, days + 1):
                clone = lifecycle_score(experiment, day)
                mass = mature**3 + Decimal(count - 1) * clone**3
                share_days += mass / (ONE + mass) - initial
            fast = registration_cost(chain, count - 1, 1)
            slow = registration_cost(chain, count - 1, chain.burn_half_life_blocks)
            rows.append(
                {
                    "identities": count,
                    "holding_days": days,
                    "incremental_fast_registration_tao": str(fast),
                    "incremental_half_life_spaced_registration_tao": str(slow),
                    "spaced_registration_duration_days": str(
                        Decimal(max(0, count - 2) * chain.burn_half_life_blocks)
                        / Decimal(ONE_DAY_BLOCKS)
                    ),
                    "incremental_miner_pool_share_days_if_all_clones_start_together": str(
                        share_days
                    ),
                    "required_surviving_miner_pool_tao_equivalent_per_day_fast_no_opex": str(
                        fast / share_days
                    )
                    if share_days
                    else None,
                    "limits": "fast/slow price models use frozen settings and approximate runtime fixed-point rounding; break-even assumes simultaneous cohort, survival, constant pool value and no fees/opex, so it cannot price the staggered slow strategy",
                }
            )
    return rows


def _epoch_group(hotkeys: list[str], snapshot: PublicSnapshot) -> dict[str, object]:
    epoch = snapshot.epoch
    index = {hotkey: uid for uid, hotkey in enumerate(epoch.hotkeys)}
    withheld = {
        uid
        for uid, coldkey in enumerate(epoch.coldkeys)
        if coldkey == epoch.owner_coldkey
    }
    uids = [index[hotkey] for hotkey in hotkeys if hotkey in index]
    pool = sum(
        value
        for uid, value in enumerate(epoch.reported_miner_alpha_rao)
        if uid not in withheld
    )
    amount = sum(
        epoch.reported_miner_alpha_rao[uid] for uid in uids if uid not in withheld
    )
    return {
        "epoch_registered_hotkeys": len(uids),
        "chain_reported_unwithheld_miner_alpha": str(Decimal(amount) / RAO),
        "chain_reported_unwithheld_miner_alpha_share": str(
            Decimal(amount) / Decimal(pool)
        )
        if pool
        else None,
    }


def benchmark_rows(experiment: Experiment) -> list[dict[str, object]]:
    """Allocation-only CPU samples; no prediction, publication, parsing, or network."""
    rows = []
    for count in (20, 254):
        forecasts = scenario(experiment, count, "exact")
        for policy in policies(experiment):
            samples = []
            for _ in range(3):
                started = process_time_ns()
                allocate(forecasts, experiment, policy)
                samples.append(process_time_ns() - started)
            rows.append(
                {
                    "policy": policy.name,
                    "subject_identities": count,
                    "cells": len(experiment.cells),
                    "cpu_nanosecond_samples": samples,
                    "minimum_cpu_ms": str(Decimal(min(samples)) / Decimal(10**6)),
                    "mean_cpu_ms": str(
                        Decimal(sum(samples)) / Decimal(len(samples) * 10**6)
                    ),
                }
            )
    return rows


def live_observations(snapshot: PublicSnapshot) -> dict[str, object]:
    chain = snapshot.chain
    index = {hotkey: uid for uid, hotkey in enumerate(chain.registered_hotkeys)}
    withheld = {
        uid
        for uid, coldkey in enumerate(chain.coldkeys)
        if coldkey == chain.owner_coldkey
    }
    scores = {row.hotkey: row.blended_score for row in snapshot.leaderboard}
    api_weights = {row.hotkey: row.api_weight_share for row in snapshot.leaderboard}
    candidate = normalize_weights(
        {
            hotkey: score
            for hotkey, score in scores.items()
            if hotkey in index and index[hotkey] not in withheld
        }
    )
    pool = sum(
        value for uid, value in enumerate(chain.incentive_u16) if uid not in withheld
    )
    rounds: list[dict[str, object]] = []
    for observed in snapshot.rounds:
        groups: dict[tuple[int, ...], list[str]] = defaultdict(list)
        for row in observed.forecasts:
            groups[row.values].append(row.hotkey)
        largest = []
        for values, hotkeys in sorted(
            groups.items(), key=lambda group: (-len(group[1]), group[0])
        )[:3]:
            uids = [index[hotkey] for hotkey in hotkeys if hotkey in index]
            incentive = sum(
                chain.incentive_u16[uid] for uid in uids if uid not in withheld
            )
            largest.append(
                {
                    "count": len(hotkeys),
                    "vector_sha256": hashlib.sha256(str(values).encode()).hexdigest(),
                    "registered_at_chain_snapshot": len(uids),
                    "distinct_coldkeys": len({chain.coldkeys[uid] for uid in uids}),
                    "current_leaderboard_share": str(
                        sum((api_weights.get(hotkey, ZERO) for hotkey in hotkeys), ZERO)
                    ),
                    "reconstructed_registered_unwithheld_candidate_share": str(
                        sum((candidate.get(hotkey, ZERO) for hotkey in hotkeys), ZERO)
                    ),
                    "observed_epoch_incentive_share_of_unwithheld": str(
                        Decimal(incentive) / Decimal(pool)
                    )
                    if pool
                    else None,
                    **_epoch_group(hotkeys, snapshot),
                }
            )
        rounds.append(
            {
                "round_id": observed.round_id,
                "submissions": len(observed.forecasts),
                "unique_numerical_vectors": len(groups),
                "unique_full_bundle_hashes": len(
                    {row.full_bundle_sha256 for row in observed.forecasts}
                ),
                "resolved_cells": sum(
                    cell.target is not None for cell in observed.cells
                ),
                "served_zero_mad_cells": sum(
                    row.mad == ZERO for row in observed.served_consensus
                ),
                "largest_groups": largest,
            }
        )
    stakes = sum(
        chain.stake_rao[uid]
        for uid, permit in enumerate(chain.validator_permits)
        if permit
    )
    validators = []
    for uid, pairs in sorted(
        chain.validator_weights.items(), key=lambda item: int(item[0])
    ):
        total = sum(value for _, value in pairs)
        validators.append(
            {
                "uid": int(uid),
                "stake_fraction_of_permitted": str(
                    Decimal(chain.stake_rao[int(uid)]) / Decimal(stakes)
                )
                if stakes
                else None,
                "owner_submitted_vote_fraction": str(
                    Decimal(dict(pairs).get(chain.owner_uid, 0)) / Decimal(total)
                )
                if total
                else None,
                "weighted_uids": len(pairs),
            }
        )
    epoch = snapshot.epoch
    epoch_pool = sum(
        value
        for uid, value in enumerate(epoch.reported_miner_alpha_rao)
        if epoch.coldkeys[uid] != epoch.owner_coldkey
    )
    return {
        "rounds": rounds,
        "validators": validators,
        "stored_scored_hotkeys": len(scores),
        "registered_scored_unwithheld_hotkeys": len(candidate),
        "withheld_uids": sorted(withheld),
        "unwithheld_epoch_incentive_u16_sum": pool,
        "reported_alpha_epoch_block": epoch.block,
        "reported_alpha_epoch_hash": epoch.block_hash,
        "reported_alpha_epoch_pool": str(Decimal(epoch_pool) / RAO),
        "uid_changes_after_epoch": sum(
            a != b for a, b in zip(epoch.hotkeys, chain.registered_hotkeys, strict=True)
        ),
        "limits": snapshot.limits,
    }
