"""Pure local counterfactuals using shipped score/EMA/consensus/encoding math."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, localcontext

from endure.aggregation.consensus import aggregate_cell, consensus_weight
from endure.assessment.schemas.subnet_alpha_risk import (
    HORIZON_5D_SECONDS,
    RISK_HORIZONS,
    RISK_SPECS_BY_OUTPUT,
    RiskOutput,
)
from endure.assessment.subnet_alpha_universe import ALPHA_RISK_WHITELISTED_NETUIDS
from endure.publication.risk_tier import RiskTierInputs, derive_risk_tier
from endure.scoring.assessment_orchestrator import score_output
from endure.scoring.context import TR_CONTEXT
from endure.scoring.emission_policy import owner_burn_weights
from endure.scoring.policy import DEFAULT_PAYOUT_HALF_LIFE_ROUNDS
from endure.scoring.weight_processing import chain_weight_vector
from endure.scoring.weights import ema_update, normalize_weights
from scripts.sybil_economics.models import Cell, Experiment, Forecast, Policy

ZERO = Decimal(0)
ONE = Decimal(1)
BPS = Decimal(10000)
FIRST_5D_RESOLUTION_DAY = 6
FIRST_30D_RESOLUTION_DAY = 31
JITTER_FRACTION_OF_GRACE = Decimal("0.25")
COORDINATED_NETUID = 8


def score_values(cells: Sequence[Cell], values: Sequence[int]) -> Decimal:
    """Mean only resolved coordinates, matching the current available-EMA blend."""
    if len(cells) != len(values):
        raise ValueError("forecast must cover the frozen cell universe")
    scores = [
        score_output(value, cell.target, RISK_SPECS_BY_OUTPUT[cell.output])
        for cell, value in zip(cells, values, strict=True)
        if cell.target is not None
    ]
    return sum(scores, ZERO) / Decimal(len(scores)) if scores else ZERO


def scale_for(cell: Cell, reference: int) -> Decimal:
    """Distance uses a public pre-commit reference, never the future target."""
    spec = RISK_SPECS_BY_OUTPUT[cell.output]
    if cell.output in (RiskOutput.TWAP_PRICE, RiskOutput.LIQUIDITY_DEPTH):
        return max(ONE, Decimal(reference) * Decimal(spec.grace_band) / BPS)
    return Decimal(spec.grace_band)


def perturb(experiment: Experiment, index: int, scaled: bool) -> tuple[int, ...]:
    """Unique valid forecasts, using only the pre-commit reference and identity index."""
    values = list(experiment.reference)
    position = index % len(values)
    cell = experiment.cells[position]
    shift = index + 1
    if scaled:
        shift += int(scale_for(cell, values[position]) * JITTER_FRACTION_OF_GRACE)
    spec = RISK_SPECS_BY_OUTPUT[cell.output]
    if spec.max_value is None:
        raise ValueError("perturbation requires an explicitly bounded wire output")
    values[position] = min(
        spec.max_value, max(spec.min_value, values[position] + shift)
    )
    return tuple(values)


def scenario(
    experiment: Experiment, copies: int, strategy: str, *, independent: bool = False
) -> tuple[Forecast, ...]:
    if any(cell.target is None for cell in experiment.cells):
        raise ValueError("oracle diagnostic requires resolved targets")
    perfect = tuple(cell.target for cell in experiment.cells if cell.target is not None)
    correct = strategy == "correct_agreement"
    competitor = experiment.reference if correct else perfect
    forecasts = [
        Forecast(
            "competitor",
            "honest",
            competitor,
            score_values(experiment.cells, competitor),
        )
    ]
    for index in range(copies):
        values = experiment.reference
        if strategy in ("near_unit", "near_scaled"):
            values = perturb(experiment, index, strategy == "near_scaled")
        elif strategy == "stale_public":
            values = experiment.stale
        elif correct:
            values = perfect
        operator = f"independent-{index}" if independent else "subject-operator"
        forecasts.append(
            Forecast(
                f"subject-{index}",
                operator,
                values,
                score_values(experiment.cells, values),
            )
        )
    return tuple(forecasts)


def _distance(left: Forecast, right: Forecast, experiment: Experiment) -> Decimal:
    return max(
        Decimal(abs(a - b)) / scale_for(cell, ref)
        for a, b, cell, ref in zip(
            left.values,
            right.values,
            experiment.cells,
            experiment.reference,
            strict=True,
        )
    )


def _groups(
    forecasts: Sequence[Forecast], experiment: Experiment, policy: Policy
) -> list[list[Forecast]]:
    groups: list[list[Forecast]] = []
    for forecast in sorted(forecasts, key=lambda row: (row.values, row.name)):
        for group in groups:
            identical = forecast.values == group[0].values
            close = policy.pooling == "near" and all(
                _distance(forecast, member, experiment) <= policy.tolerance
                for member in group
            )
            if identical or close:
                group.append(forecast)
                break
        else:
            groups.append([forecast])
    return groups


def _pool(
    contributions: dict[str, Decimal], groups: Sequence[Sequence[Forecast]]
) -> dict[str, Decimal]:
    pooled = dict(contributions)
    for group in groups:
        budget = max(contributions[row.name] for row in group)
        total = sum((contributions[row.name] for row in group), ZERO)
        for row in group:
            pooled[row.name] = (
                budget * contributions[row.name] / total if total else ZERO
            )
    return pooled


def allocate(
    forecasts: Sequence[Forecast], experiment: Experiment, policy: Policy
) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    """Return rewards and publication contributions; ownership labels are unused."""
    with localcontext(TR_CONTEXT):
        scores = {row.name: row.score for row in forecasts}
        if policy.baseline_score is not None:
            scores = {
                key: max(ZERO, value - policy.baseline_score)
                for key, value in scores.items()
            }
        earned = {key: value**policy.gamma for key, value in scores.items()}
        publication = {
            row.name: consensus_weight({row.name: row.score}, row.name)
            for row in forecasts
        }
        if policy.pooling != "none":
            groups = _groups(forecasts, experiment, policy)
            earned = _pool(earned, groups)
            publication = _pool(publication, groups)
        # Contributions have already been sharpened; gamma=1 normalizes once.
        return normalize_weights(earned, gamma=1), publication


def consensus_result(
    forecasts: Sequence[Forecast],
    experiment: Experiment,
    contributions: dict[str, Decimal],
) -> dict[str, object]:
    medians: dict[tuple[int, int, RiskOutput], Decimal] = {}
    rows: list[dict[str, object]] = []
    for position, cell in enumerate(experiment.cells):
        median, mad, count = aggregate_cell(
            [(row.values[position], contributions[row.name]) for row in forecasts]
        )
        medians[(cell.netuid, cell.horizon_seconds, cell.output)] = median
        rows.append(
            {
                "netuid": cell.netuid,
                "horizon": cell.horizon_seconds,
                "output": cell.output.value,
                "median": str(median),
                "mad": str(mad),
                "n_submitters": count,
            }
        )
    tiers: dict[str, str] = {}
    for netuid in sorted({cell.netuid for cell in experiment.cells}):
        horizon = next(
            (
                cell.horizon_seconds
                for cell in experiment.cells
                if cell.netuid == netuid and cell.horizon_seconds != HORIZON_5D_SECONDS
            ),
            None,
        )
        tiers[str(netuid)] = derive_risk_tier(
            RiskTierInputs(
                medians.get((netuid, horizon, RiskOutput.MAX_DRAWDOWN))
                if horizon is not None
                else None,
                medians.get((netuid, horizon, RiskOutput.REALIZED_VOLATILITY))
                if horizon is not None
                else None,
            )
        )
    return {"rows": rows, "diagnostic_30d_tiers": tiers}


def coordinated_scenario(
    copies: int, perturbed: bool
) -> tuple[Experiment, tuple[Forecast, ...]]:
    """Synthetic grace-band counterexample; targets are an explicit oracle diagnostic."""
    target_values = {
        RiskOutput.MAX_DRAWDOWN: 2000,
        RiskOutput.REALIZED_VOLATILITY: 7000,
        RiskOutput.TWAP_PRICE: 100_000_000,
        RiskOutput.LIQUIDITY_DEPTH: 1_000_000_000_000,
    }
    cells = tuple(
        Cell(
            netuid=netuid,
            horizon_seconds=horizon,
            output=output,
            target=target_values[output],
        )
        for netuid in ALPHA_RISK_WHITELISTED_NETUIDS
        for horizon in RISK_HORIZONS
        for output in RiskOutput
    )
    truth = tuple(target_values[cell.output] for cell in cells)
    experiment = Experiment("synthetic_coordinated_grace_band", cells, truth, truth)
    position = next(
        index
        for index, cell in enumerate(cells)
        if cell.netuid == COORDINATED_NETUID
        and cell.output == RiskOutput.MAX_DRAWDOWN
        and cell.horizon_seconds != HORIZON_5D_SECONDS
    )
    forecasts = []
    for index, value in enumerate((2000, 2050)):
        values = list(truth)
        values[position] = value
        forecasts.append(
            Forecast(
                f"honest-{index}",
                f"independent-{index}",
                tuple(values),
                score_values(cells, values),
            )
        )
    for index in range(copies):
        values = list(truth)
        values[position] = 1950 + (10 * index if perturbed else 0)
        forecasts.append(
            Forecast(
                f"subject-{index}",
                "subject-operator",
                tuple(values),
                score_values(cells, values),
            )
        )
    return experiment, tuple(forecasts)


def chain_result(
    forecasts: Sequence[Forecast], earned: dict[str, Decimal], burn_bps: int, size: int
) -> dict[str, object]:
    """Actual release burn/processing/encoding; synthetic UID assignment is explicit."""
    if len(forecasts) >= size:
        raise ValueError("scenario exceeds miner plus owner UID capacity")
    scores = [ZERO] * size
    for uid, row in enumerate(forecasts, 1):
        scores[uid] = earned[row.name]
    raw = owner_burn_weights(scores, owner_uid=0, burn_bps=burn_bps)
    if raw is None:
        raw = tuple(ONE if uid == 0 else ZERO for uid in range(size))
    vector = chain_weight_vector(
        raw,
        uids=range(size),
        metagraph_size=size,
        min_allowed_weights=1,
        max_weight_limit=ONE,
    )
    encoded = dict(zip(vector.uint_uids, vector.uint_weights, strict=True))
    subject = sum(encoded.get(uid, 0) for uid in range(2, len(forecasts) + 1))
    all_units = sum(vector.uint_weights)
    miner_units = all_units - encoded.get(0, 0)
    return {
        "burn_bps": burn_bps,
        "candidate_total_vote_subject": str(
            sum((earned[row.name] for row in forecasts[1:]), ZERO)
            * (ONE - Decimal(burn_bps) / BPS)
        ),
        "uint_pairs": list(zip(vector.uint_uids, vector.uint_weights, strict=True)),
        "encoded_subject_total_vote_share": str(Decimal(subject) / Decimal(all_units)),
        "encoded_subject_miner_pool_share": str(Decimal(subject) / Decimal(miner_units))
        if miner_units
        else None,
        "encoded_burn_fraction": str(Decimal(encoded.get(0, 0)) / Decimal(all_units)),
        "rounded_zero_subjects": sum(
            encoded.get(uid, 0) == 0 for uid in range(2, len(forecasts) + 1)
        ),
    }


def lifecycle_score(experiment: Experiment, day: int) -> Decimal:
    """Synthetic repeated fixture, new miner day zero; horizons resolve independently."""
    values: list[Decimal] = []
    for cell, submitted in zip(experiment.cells, experiment.reference, strict=True):
        due = (
            FIRST_5D_RESOLUTION_DAY
            if cell.horizon_seconds == HORIZON_5D_SECONDS
            else FIRST_30D_RESOLUTION_DAY
        )
        rounds = max(0, day - due + 1)
        if not rounds or cell.target is None:
            continue
        score = score_output(submitted, cell.target, RISK_SPECS_BY_OUTPUT[cell.output])
        ema = ZERO
        for _ in range(rounds):
            ema = ema_update(
                ema, score, half_life_rounds=DEFAULT_PAYOUT_HALF_LIFE_ROUNDS
            )
        values.append(ema)
    return sum(values, ZERO) / Decimal(len(values)) if values else ZERO
