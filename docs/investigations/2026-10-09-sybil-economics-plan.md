# Endure Sybil economics — investigation plan

**Status: first pass completed on 2026-10-13; historical planning record.**
See the [decision report](2026-10-13-sybil-economics.md). The user confirmed one
investigator-day, and evidence locations/acceptance guidance were inferred from
existing sources. No numeric production threshold was approved.

Apeguru owns the decision. This plan covers
Alpha Risk V1 only and proposes a first pass of one investigator-day, including
review and reporting. That effort cap is provisional. No mitigation or acceptance
threshold has been selected.

The question is whether additional identities can buy additional rewards or
consumer influence without additional predictive information, and whether a
small intervention can reduce that advantage without making correct independent
agreement uneconomic. The permitted outcome includes recommending no production
change when the evidence does not justify one.

## Decisions already supplied

- Forge launch is the primary priority for the next two weeks; this is a bounded
  secondary track led by Apeguru with the coding fleet.
- Investigate exact replicas, perturbed replicas, information copying/reuse, and
  coordinated distortion. Independent honest agreement is a required control.
- Use local simulation/replay and authorized read-only evidence. No production
  scoring edits, chain transactions, deployments, burn changes, or expenditure.
- Compare different mitigation mechanisms before recommending an intervention.
  Similarity is not evidence of common ownership. No identity mechanism, cap,
  or clustering rule is presumed to be the answer.
- Apeguru approves acceptance criteria; any eventual production change needs
  separate approval. Schema changes, dependencies, and protocol bumps remain
  outside this investigation.

## Interview choices open during planning

| Choice | Why it changes the work | Provisional handling |
| --- | --- | --- |
| Hard total effort cap | Controls replay size and prototype depth without drawing effort from Forge | One investigator-day, interpreted as eight focused hours of total fleet effort, including review; confirm the interpretation |
| Live validator URLs or existing non-secret evidence paths | Needed to distinguish running policy/state from published software | Mainnet configuration remains unknown until independently evidenced |
| Existing economic and consumer tolerances | Determines whether a measured tradeoff is acceptable | Report curves and propose criteria; do not call a research parameter an approved threshold |

The interview offers an inventory-and-plan-only first step as well. If selected,
stop at this document; do not begin the simulation tranche.

## Evidence inventory verified during planning

The clean worktree is on `audit-sybil-protection`, at commit
`4376ff42a6b619ed995144b63371ec2e82125316`. Read-only remote inspection on
2026-10-09 found `main` at that commit, `develop` at
`672317e4a0935f4de889c363da654fa08f577dd4`, and `staging` at
`cd847c4bff4052543a22957dade4470786e3ea66`. Pin revisions again when running
experiments; branches and image channels can move.

| Evidence class | Established now | Limits |
| --- | --- | --- |
| Historical calibration | The committed RC1 report records approximately 35.65%, 62.44%, and 73.48% for one, three, and five identical reference forecasts against one perfect forecaster | A deterministic fixture calculation, not observed mainnet exploitation |
| Calibration implementation | `scripts/calibrate_alpha_scoring.py` calculates mean coordinate scores for netuids 8 and 44, both horizons and four outputs; its share formula is `n × score³ / (1 + n × score³)` | Sixteen coordinates at one fixture anchor; no sequential EMA replay, chain limits, u16 encoding, owner burn, or realized payouts. Reporting the half-life does not mean the script simulates it |
| Repository reward policy | `normalize_weights` cubes each hotkey's blended score independently and normalizes the sum; the payout half-life is five rounds | This establishes code behavior at the pinned revision, not which version or histories each live validator uses |
| Repository publication policy | `consensus_weight` uses `max(blended score, 0.01)` per submitting hotkey; each coordinate publishes a weighted median, weighted MAD, and submitter count | Publication influence differs from cubic payout weight, and a newcomer can affect publication before earning a positive resolved score |
| Repository admission | Served mainnet/testnet policy pins the additional miner stake floor to zero; registration and valid commit/reveal are still required | No additional application stake floor does not mean zero registration, collateral, transaction, capital, or operating cost |
| Published release | Public v0.1.2 is key 2043, tagged at `cd847c4bff4052543a22957dade4470786e3ea66`, with owner burn allocation and chain emission safeguards | The release explicitly separates publication from mainnet canary/third-party cutover; it does not establish today's deployed version or burn rate |
| Deployment and costs | SN30 is the documented mainnet netuid | Current per-validator runtime/configuration, scoring state, burn commitment, chain constraints, costs, and realized attack rewards have not been measured |

Historical fixture SHA-256:
`6548accf983b3d4e280a3740fb0b6690803e3169db6043fdab2b6c0add3f31fc`.
Anchor block: `7410000`. The planning pass inspected the report and its code;
an executable reproduction is still a task below.

## First-pass schedule and stopping rules

| Tranche | Proposed effort | Concrete output and stop condition |
| --- | --- | --- |
| Freeze policy and evidence | 1 hour | Evidence manifest, runtime/chain inventory, historical reproduction. Stop chasing unavailable access after this tranche; preserve unknowns and continue with explicitly conditional repository scenarios |
| Build and run exposure replay | 3 hours | Deterministic baseline/attack/control rows, identity-count curves, maturation and publication results |
| Compare local mitigation variants | 2 hours | Same scenario matrix for at least three materially different mechanisms, plus a counterexample for each |
| Review and decision report | 2 hours | At most one bounded reviewer, with its effort counted in this cap; reproducibility check, findings, recommendation or no-change rationale, proposed acceptance criteria |

Apeguru receives a checkpoint after the evidence inventory. Reserve the final
two hours for review/reporting even if replay data is incomplete. Do not start a
30-day collection campaign, train a new forecasting model, undertake an identity
research program, or pull Forge owners into implementation. Missing live history
limits the conclusion; it does not authorize expanding the investigation.

## Read-only evidence to freeze

Capture provenance, timestamps, hashes, versions, and missingness for every input.
Public evidence is preferred. Do not put endpoint credentials, wallet material,
private configuration, or embargoed submissions in public artifacts.

- For each evidenced validator: `/health`, `/schemas`, published round metadata
  and universes, every page of post-embargo submissions, outcomes, score history,
  consensus, and the risk feed. Record package/protocol/source/content identities
  and emission mode/burn status. Health/source claims do not alone prove the
  container image digest; record an independently supplied digest if available.
- For exact replay: frozen accepted-bundle snapshot, canonical realized targets,
  pre-round coordinate EMAs, expected/active/historical eligibility, registration
  observations, and the ordered UID/hotkey mapping. Reconstruct consensus at its
  publication time rather than retroactively weighting it with later scores.
- At a named finalized Finney block/hash: genesis, runtime spec/version/metadata,
  owner identity and burn commitment, registered slots and available capacity,
  validator stakes/permits and submitted weight matrix, relevant epoch state,
  chain minimum/maximum weight constraints, and actual emission/incentive data.
- Registration economics: current price and the runtime's actual price dynamics,
  minimum/maximum price, collateral/refund rules if present, admission/rate
  constraints, immunity, pruning/replacement, and registration/transaction fees.
  Documentation or SDK defaults cannot substitute for SN30 state. Registration
  burn and the owner's emission burn are different quantities.

`scripts/validator_evidence.py` helps compare redacted consensus, target, EMA,
and emission records, but it does not provide the complete pre-round replay
inputs. It explicitly leaves target block provenance null. Treat it as a partial
source, and use an existing offline snapshot or public post-embargo endpoints to
fill gaps. Never open the production database through a tool that can initialize
or migrate it; work on an authorized consistent copy if one exists.

Current chain registration rules must be selected from the observed runtime.
The official miner and hyperparameter documentation describes mechanisms and
defaults; those are not measurements of SN30's values.

## Reproducible exposure experiment

Use Python 3.12, the locked project environment, integer wire units, and Decimal
economics. The current worktree has no `.venv`; its default `python3` is 3.11.2,
but `uv python find 3.12 --no-python-downloads` found an installed interpreter.
Prepare the supported environment using the existing lockfile without adding
dependencies. Use existing scoring, EMA, consensus, tier, burn, and weight
encoding functions without editing production implementations.

First reproduce the committed calibration and compare canonical JSON exactly.
Then exercise the current policy sequentially. Distinguish fixture-only,
synthetic lifecycle, public-data replay, and complete validator-state replay
results. A synthetic extension of a historical fixture is not live history.

| Scenario | Comparison and attack construction |
| --- | --- |
| No attack | Fix honest population, their forecasts and histories, and one operator identity; repeat for heterogeneous skill and agreement populations |
| Exact replication | Add identities with identical numerical forecasts, varied nonces/signatures, and controlled equal-age or new-entry histories; do not identify duplicates by commitment hash |
| Perturbed replication | Try single-unit changes, output-scaled jitter, sparse coordinate changes, and adaptive changes just beyond each candidate detector's boundary; preserve schema validity and causality |
| Copying and delayed reuse | Use only information demonstrably available before that round's commitment cutoff: public baselines, previously released forecasts, stale forecasts, and market observations. Reconstruct actual embargo/window availability |
| Coordinated output distortion | Move a chosen coordinate toward a valid attack target; vary participation, score sacrifice, and established versus newcomer histories; trace both forecast error and published tier changes |
| Honest agreement controls | Exact and near agreement by independently labelled operators, including correct high-skill agreement and agreement caused by a shared public signal; no intervention sees the simulated ownership labels |
| Better forecasting | Causal higher-skill predictors compete against replica groups; a perfect foresight forecaster is a diagnostic upper bound, never a deployable strategy |

Start with identity counts 1, 2, 3, 5, 10, and 20, then include the observed
feasible slot boundary. Counts exceeding evidenced capacity remain hypothetical.
These are experimental sample points, not policy limits. Test cold start,
equal-age growth, mature honest miners versus new clones, staggered registration,
missing coordinates/rounds, and deregistration/archival. Locally advance time
through the first 5-day and 30-day resolutions and EMA maturation; do not wait
for those horizons on mainnet.

Scale feasible perturbations from pre-commit observations and schema units.
If realized targets are used to construct a best-case evasion example, label it
an oracle diagnostic and exclude it from feasible attack profitability. Tune
variants on one segment and evaluate on a held-out segment when real history
permits; otherwise explicitly limit generalization.

Exact replicas and honest agreement can have identical observable inputs. Keep
matched controls with equal forecasts, histories, and timing but different
operator labels. Any forecast-only rule must produce the same result for that
pair; ownership detection is not identifiable from agreement alone.

## Measurements and economic interpretation

For equal-history replicas with positive blended score `s`, and fixed honest
sharpened score mass `H`, the repository candidate operator share is
`R(n) = n × s³ / (H + n × s³)`. Verify this against the actual pipeline, then
measure cases where histories differ instead of assuming equal scores.

| Measure | Reported quantity |
| --- | --- |
| Reward amplification | Aggregate operator share `R(n)`, `R(n)/R(1)` when defined, marginal gain per new identity, per-identity share, and honest share lost against the fixed no-attack population |
| Publication influence | Per-output/native-unit and normalized-error shifts in medians; MAD changes, including misleadingly narrow dispersion; submitter-count inflation; A–E tier changes and the identity count needed to move the median |
| Chain transformation | Candidate earned weights, owner allocation, chain-processed weights, exact submitted u16 entries, rounded-zero miners, and realized normalized burn/miner fractions |
| Realized reward | Separate chain/Yuma incentives and paid emission from validator candidates; account for validator stake, heterogeneous votes, clipping, withholding, and the observed runtime's emission rules |
| Cost and profitability | Sunk registration/fees, locked capital and opportunity cost, time before reward, slot replacement/re-registration, shared forecasting cost, incremental signing/transport/storage, and operating-cost ranges |
| Honest participation | Per-operator and per-miner payout loss from an intervention, retention/pruning exposure, break-even operating cost, and false intervention on independent correct agreement |
| Better forecasts | Maximum replica count a causal higher-skill miner can economically compete with, accuracy premium, maturation delay, and reward per unit of measured skill gain |
| Engineering cost | Local benchmark runtime/memory and scaling with miners/coordinates/history, required state, dependencies, versioning needs, operational knobs, and failure/rollback behavior |

Maintain a waterfall from earned candidate to owner burn to chain processing to
u16 to Yuma/incentive to paid assets. Do not equate one validator's weights with
rewards. A high burn can reduce the absolute pool while leaving replication
advantage within that pool; rounding can change that advantage. Owner burn can
also affect network allocation to the subnet: verify the runtime before valuing
the resulting miner revenue. Unknown burn, emission pool, or conversion prices
stay unknown. Synthetic burn settings are labelled sensitivities, never asserted
as deployment configuration.

Where actual reward reconstruction is unavailable, report conditional returns
and break-even registration/operating-cost curves instead of a purported live
ROI. State the holding horizon and asset units; distinguish Alpha earnings from
TAO value and include conversion/slippage assumptions only when evidenced.

## Mitigation comparison, without a predetermined winner

Run the same baseline/attack/control matrix on at least three materially
different mechanisms. All variants are local experiment code. Reporting a
variant's result does not authorize installing it.

| Mechanism to investigate | Hypothesis and required counterexample |
| --- | --- |
| Forecast budget pooling | Share a fixed contribution budget among exact-equal forecasts; separately test approximate pooling over histories. Measure reward and publication changes, near-replica escape, split/merge instability, and losses for independent correct agreement. Test canonical values rather than nonce-dependent hashes |
| Incremental predictive skill | Reward improvement over a causal public baseline or a measured marginal-information benchmark. Test whether a positive residual reward still multiplies across replicas, whether flat/noisy markets suppress honest rewards, and whether peer-relative versions permit collusion |
| Participation and capital friction | Model registration/collateral/stake or track-record costs as economic controls, without changing them. Test amortization and evasion through more coldkeys, unequal capital access, newcomer penalties, and whether the measured expense actually bounds profitable replication |
| Publication guard only | Test requiring resolved predictive evidence before full publication influence, or flagging concentration for consumers. Measure first-round influence and cold-start availability; establish explicitly that a publication-only guard leaves reward replication unresolved |

Sharpening and per-hotkey caps are useful diagnostic comparators: stronger
sharpening may favor better forecasts yet leave every positive-score strategy
replicable; an identity cap may encourage splitting. Timing/embargo controls
must be tested for stale-publication reuse but cannot be credited with identity
independence. These are hypotheses to verify, not chosen defenses.

For every mechanism record reward amplification, risk-output effects,
false-positive economic harm, adaptive evasion, implementation/operating cost,
and rollout risk. Decline a production recommendation if apparent protection
depends on oracle ownership labels, punishes correct agreement beyond accepted
limits, or merely pushes the attack just outside a tested similarity threshold.

## Proposed acceptance criteria for Apeguru

The following are measurable approval questions, not settled production policy.
Present the observed curves and feasible tradeoffs before asking Apeguru to set
numerical tolerances.

- Reproducibility: historical JSON matches; identical pinned input manifests
  yield identical Decimal score transitions, consensus/tier values, and u16
  vectors. Labels/reordering do not change forecast-only decisions.
- Exposure bound: set an acceptable `R(n)/R(1)` and marginal-return bound over
  evidenced feasible identity counts and histories. Exact-replica invariance
  (`R(n) = R(1)`) is a candidate research target, not an approved rule. Evaluate
  near-replica evasion under an agreed perturbation/score-loss budget as well.
- Consumer harm: approve per-output deviation limits and a tier-change budget,
  including cold start and MAD/count misrepresentation. Use reported native
  units and schema scoring scales rather than an invented shared distance.
- Honest viability: approve maximum payout loss for independent correct
  agreement and an operating-cost/retention envelope. Compare against that
  same honest population without the candidate intervention.
- Forecasting premium: approve a minimum economic advantage for held-out causal
  skill improvements over accessible baselines, including maturation time.
- Practicality: approve a bounded runtime/state budget and the smallest rollout
  slice. Any scoring/consensus change must name its later compatibility and
  protocol-review requirements. Evidence coverage and missingness must support
  the claimed bound; insufficient coverage yields a no-change recommendation.

The decision report will recommend one bounded follow-up or explain why none is
justified. If only a shadow experiment is credible, specify its data, duration,
stop condition, and decision metric. It does not silently become a production
anti-Sybil rollout.

## Deliverables and verification

Produce a versioned local simulation/replay entry point, a hashed input manifest,
machine-readable scenario results with Decimal strings, and a concise decision
report with a comparison table. Keep the historical reproduction separately
labelled. Preserve the exact invocation, locked environment identity, seeds,
round cutoffs, population histories, perturbations, intervention parameters,
and all evidence gaps.

Use focused tests for causality, exact/honest matched controls, conservation of
allocation, identity/order invariance, and a known evasion counterexample. Run
the applicable local checks; `make verify` is mandatory before any later push.
No schema migration, dependency, or protocol change is needed to investigate.
The rendered review artifact is disposable; reproducible code/data/report, if
produced, belong in a focused investigation change against `develop`.

## Sources inspected

- [Economic limitations at the inspected revision](https://github.com/endure-network/endure-subnet/blob/4376ff42a6b619ed995144b63371ec2e82125316/docs/economic-limitations.md)
- [Historical calibration report](https://github.com/endure-network/endure-subnet/blob/4376ff42a6b619ed995144b63371ec2e82125316/docs/calibration/alpha-risk-v1-rc1.md), [calibration script](https://github.com/endure-network/endure-subnet/blob/4376ff42a6b619ed995144b63371ec2e82125316/scripts/calibrate_alpha_scoring.py)
- [Reward math](https://github.com/endure-network/endure-subnet/blob/4376ff42a6b619ed995144b63371ec2e82125316/endure/scoring/weights.py), [publication math](https://github.com/endure-network/endure-subnet/blob/4376ff42a6b619ed995144b63371ec2e82125316/endure/aggregation/consensus.py)
- [Mainnet runbook](https://github.com/endure-network/endure-subnet/blob/4376ff42a6b619ed995144b63371ec2e82125316/docs/running_on_mainnet.md), [v0.1.2 public release](https://github.com/endure-network/endure-subnet/releases/tag/v0.1.2)
- [Evidence exporter](https://github.com/endure-network/endure-subnet/blob/4376ff42a6b619ed995144b63371ec2e82125316/scripts/validator_evidence.py), [public read endpoints](https://github.com/endure-network/endure-subnet/blob/4376ff42a6b619ed995144b63371ec2e82125316/endure/api/app.py)
- [Official miner lifecycle documentation](https://guides.learnbittensor.org/miners), [official chain hyperparameters](https://guides.learnbittensor.org/subnets/subnet-hyperparameters) — mechanism references only; runtime/values must still be observed
