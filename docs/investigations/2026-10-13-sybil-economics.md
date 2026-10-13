# Endure Sybil economics — bounded first-pass decision report

**Decision: no automatic reward penalty or consensus change is justified by this
first pass.** Replication has a material economic and publication advantage under
the observed v0.1.2 policy. The tested penalties either admit inexpensive
perturbations, retain replication inside a similarity group, or reduce rewards
for independent correct agreement. Keep this track bounded while Forge launches;
use the delivered offline concentration ledger and experiments to evaluate a
subsequent, separately approved intervention.

This recommendation selects no production mechanism. The investigation changed
local analysis, evidence, tests, and documentation only. It submitted no mainnet
transaction, spent no funds, and changed no scoring, burn, protocol, or deployment
setting. One investigator-day was the confirmed cap; one read-only fleet reviewer
checked evidence and economics. No ongoing collection campaign was started.

## What is established, and what is not

An operator with a positive score can increase its aggregate candidate weight by
adding equally scored identities, without adding a forecast. The release also
adds publication weight per hotkey. Small valid changes make numerical forecasts
distinct while preserving almost all of this reward advantage. These are
mechanical properties reproduced locally, not claims of a detected operator.

Live evidence shows repeated information is already economically material:
**107 and 52 hotkeys submitted two identical full-bundle groups** in the
2026-10-11 published round. Those same hotkeys accounted for **82.57% of the
remaining miner Alpha allocation reported by the captured epoch event**. The
groups span 31 and 2 current registered coldkeys. Neither different coldkeys nor
identical forecasts establish independent operation, common ownership, intent,
unauthorized copying, or useful incremental information.

| Evidence layer | Verified observation | Interpretation limit |
| --- | --- | --- |
| Historical fixture | Original calibration JSON reproduces exactly; five reference copies receive 73.4782% against one oracle-perfect forecaster | Two netuids, sixteen coordinates, one fixture anchor; not observed mainnet exploitation or a sequential payout replay |
| Repository | Cubic per-hotkey weights, five-round coordinate EMA, blend-weighted median/MAD, newcomer publication floor 0.01 | Mathematical behavior of the pinned source; equal histories are an explicit counterfactual |
| Deployed team validator | Public UID 195 API reports package 0.1.2, key 2043, source `cd847c4…`, content `bf64934…`, matching the public release | Runtime self-reports; image digest and other validators' source/configuration remain unverified |
| Current chain | Finalized block 9271786, runtime spec 477; owner commitment 9800 bps, 256 occupied slots; min/max weight constraints 1/1 | A dated snapshot, not a standing configuration guarantee |
| Epoch output | Block 9271517 reports 2.582823142 Alpha for unwithheld miner recipients; attribution uses that epoch's UID mapping | Chain-reported miner allocation, not audited wallet credits, spendable proceeds, or conversion execution |
| Proposed changes | Local exact/near pooling, public-baseline excess skill, sharpening, and cost sensitivities | Experimental alternatives, with no approved numerical policy thresholds |

Mainnet evidence was inferred from existing deployment documentation rather than
requested from the owner. The documented endpoint is
[UID 195's read API](https://endure-sn30-uid195.tools.defizoo.fi/health).
The [public release](https://github.com/endure-network/endure-subnet/releases/tag/v0.1.2)
is a release record, not proof that every validator upgraded. The captured health
reported `abstain / chain_snapshot_inconsistent`, burn status null, and last
confirmed weights at 03:01:05 UTC on October 13. The separately read owner
commitment is 9800; null health burn is not a missing chain commitment. Abstention
does not clear prior weights.

Full chain snapshot hash:
`0x9fb4502caefdc98e489216fbe7e4d7f337e300a4e3b629e0b6b27ee69b9b68a9`,
retrieved 2026-10-13 at 04:01:22 UTC.

## Observed repetition and allocation

Complete paging of public post-embargo submissions gives:

| Round | Accepted forecasts | Unique numerical vectors | Unique full bundles |
| --- | ---: | ---: | ---: |
| 2026-09-28 | 1 | 1 | 1 |
| 2026-10-03 | 2 | 2 | 2 |
| 2026-10-05 | 202 | 29 | 29 |
| 2026-10-10 | 208 | 38 | 38 |
| 2026-10-11 | 208 | 41 | 41 |

Fingerprints use sorted numerical coordinates, not commitment digests or nonces.
Full-bundle hashes additionally capture confidence and reason metadata. Adding
distinct nonces does not add predictive information. Conversely, similar
forecasts do not identify an owner.

The October 11 groups have different shares at different economic layers:

| Group size | Current leaderboard | Reconstructed registered, unwithheld candidate | Stored Incentive share of unwithheld UIDs | Reported epoch miner Alpha | Share of unwithheld reported miner Alpha |
| --- | ---: | ---: | ---: | ---: | ---: |
| 107 | 46.64% | 47.14% | 54.37% | 1.402734048 | 54.31% |
| 52 | 31.98% | 32.33% | 28.66% | 0.729998988 | 28.26% |
| Combined | 78.62% | 79.46% | 83.03% | 2.132733036 | 82.57% |

The API leaderboard contains 228 scoring records; 213 are scored, registered and
unwithheld in the chain snapshot. Its normalization has no current registration
filter, so it is not the emitted candidate. The reconstruction freezes API
scores and applies the observed mapping/withholding; it is not a recovered
historical validator attempt. Publication-time reputations are unavailable.

Two UIDs changed between the epoch and snapshot. The Alpha column uses the
epoch-block mapping rather than transplanting current UIDs onto old emissions.
Stored Incentive is a rounded chain state vector; the event reports actual Alpha
amounts at that layer. Different validator inputs, histories, stake, clipping,
rounding, and timing prevent equating these columns. Current permitted-stake
share for UID 116 is approximately 39.89%; the older deployment record's 71.5%
was not substituted for today's state. Peer source identities remain unknown.

The observed feed has fifteen netuids, all `unrated`; no 30-day outcomes have
resolved. The captured October 11 consensus has no zero-MAD cells. The synthetic
confidence and tier counterexamples below are not observations of live feed
distortion.

## No-attack baseline, replication, perturbation and reuse

For positive equal-history blend `s` and fixed honest sharpened mass `H`,
`R(n) = n × s³ / (H + n × s³)`. Reward is additive in identity count before
normalization, and saturates as the operator captures the pool. Per-identity
reward falls while aggregate operator reward rises.

The seeded baseline is one operator identity and one oracle competitor. Other
honest forecasts/histories stay fixed when adding identities. Oracle predictions
are diagnostic upper bounds, never deployable strategies. Static resolved-score
comparisons approximate equal-age/mature records; the separate lifecycle table
starts new identities from zero. Current full registration means extra real
identities require replacement, and cannot simply be appended to the live graph.

| Total operator identities | Historical fixture share | Amplification vs one | October 5 five-day sample share | Amplification vs one |
| --- | ---: | ---: | ---: | ---: |
| 1, no replication | 35.65% | 1.00× | 46.30% | 1.00× |
| 2 | 52.57% | 1.47× | 63.30% | 1.37× |
| 5 | 73.48% | 2.06× | 81.17% | 1.75× |
| 20 | 91.72% | 2.57× | 94.52% | 2.04× |
| 107 | 98.34% | 2.76× | 98.93% | 2.14× |

The newer sample uses sixty resolved five-day coordinates across all fifteen
netuids on October 5. Its largest exact forecast group averages **0.951818** raw
score. This is one selected high-activity round, not a representative estimate
of long-run forecasting skill or a full validator-state replay.

| Strategy, five identities against one oracle | Historical mean score / share | October 5 mean score / share |
| --- | ---: | ---: |
| Exact seed replicas | 0.821350 / 73.4782% | 0.951818 / 81.1731% |
| Unique single-unit perturbations | 0.821335 / 73.4771% | 0.951816 / 81.1730% |
| Sparse perturbations near one-quarter of a public-reference grace scale | 0.821012 / 73.4541% | 0.951787 / 81.1715% |
| Stale public forecast reuse | 0.822206 / 73.5390% | 0.869548 / 76.6758% |

Perturbations use only the seed, schema bounds, a pre-commit reference scale and
identity index. A test removes future targets and confirms the perturbations
stay identical. The live seed was selected afterward by group size; it is an
operator-owned-seed counterfactual, not proven publicly available before that
round's commitment. Stale reuse is allowed by the stored embargo schedule:
October 3's scheduled publication precedes October 5's cutoff. Contemporaneous
availability/uptime was not observed. The most recent round remains embargoed
through the next commitment close, so it cannot automatically be treated as a
pre-commit public source. Older public information remains reusable.

A perfect five-day forecaster has only about **1.16×** the per-identity cubic
mass of the 0.951818 seed. Two equal-age seed replicas beat its aggregate mass;
with five, the oracle receives only 18.83%. Higher forecasting quality is
rewarded per hotkey, but cannot defeat enough positive-score copies. Stored EMA
rank also reflects tenure and absence; it is not a pure skill ranking.

## Publication influence and honest agreement

Publication weights use the blend directly, floored at 0.01, rather than cubing
it. Repetition can therefore win the weighted median independently of payout
share. With one mature score-one forecaster, 101 unproven replicas contribute
1.01 publication mass before earning any score. This is a conditional example;
feasible replacements and the remaining live honest mass must still be included.

Changing one seed to five exact copies moves the following published values in
the equal-history, one-oracle counterfactual. Each maximum spans that sample's
netuids and horizons; these are not measured changes in the live feed.

| Output | Historical fixture maximum change | October 5 five-day sample maximum change |
| --- | ---: | ---: |
| Drawdown | 2122 bps | 605 bps |
| Volatility | 7067 bps | 4223 bps |
| TWAP price | 7,432,415 RAO/Alpha; maximum relative change 22.60% | 6,518,638 RAO/Alpha; maximum relative change 12.42% |
| Liquidity depth | 7,513,733,525,710 RAO; maximum relative change 14.98% | 4,583,277,544,257 RAO; maximum relative change 5.41% |

A synthetic control covers the full 120-coordinate whitelist and holds two
honest 30-day drawdown forecasts for netuid 8 at 2000 and 2050 bps, volatility at
7000 bps, and other coordinates constant. One
operator adds forecasts at 1950, 1960 and 1970 bps. All are within the shipped
200-bps drawdown grace band and score one against the diagnostic target.

| Operator forecasts | Reward share | Published drawdown | MAD | Derived tier |
| --- | ---: | ---: | ---: | --- |
| One, with two honest forecasters | 33.33% | 2000 bps | 50 bps | C |
| Three exact copies at 1950 | 60.00% | 1950 bps | 0 bps | B |
| Three distinct forecasts, 1950/1960/1970 | 60.00% | 1970 bps | 20 bps | B |

The distinct version defeats exact and tight pooling by separating forecasts.
Wide pooling detects their similarity and groups all five forecasts, but then
preserves their 3:2 internal majority. It still publishes 1970 bps and B. This is
failure despite detection, not successful evasion of the wide detector. Existing
tier boundaries are acknowledged informed guesses; this example shows the
mechanical discontinuity, not a validated safety tolerance.

Independent correct agreement is an additional control. Five independent
oracle-perfect forecasts compete with one distinct reference forecast. Exact
or near pooling cuts each correct forecaster's reward by **28.52%** on the old
fixture and **37.04%** on the newer five-day sample. Matched Sybil and independent
controls have identical observable forecasts, histories and timing. Every tested
forecast-only rule necessarily treats them alike; ownership labels never enter
the allocation function.

For the newer control, each honest forecaster's pool share falls from 17.06% to
10.74%. If daily honest operating cost lies between `0.1074 × M` and
`0.1706 × M`, where `M` is the surviving miner pool's daily value, the prototype
turns previously viable honest operation uneconomic. Actual modeling and
operating costs have not been measured; this is a quantified viability envelope.

## Costs, burn and the path to actual rewards

At the observed block, miner admission has no additional application stake
floor; chain registration is enabled, PoW registration is disabled, collateral
lock share is zero, and all slots are occupied. Registration costs TAO and is
sunk. The current quote is **0.109689336 TAO**, bounded by a **0.1 TAO** floor and
**100 TAO** ceiling. Observed price multiplier is approximately 1.26, price
half-life 360 blocks, maximum registrations one per block, and immunity 50,400
blocks, approximately seven days at twelve-second blocks.

Illustrative price paths use these frozen settings and approximate runtime
fixed-point rounding. They start with one existing mature operator identity;
only new clones are purchased. Registration fees, actual pruning outcomes,
future owner settings, competition, and survival are additional uncertainties.

| Total identities | New registrations | Cost at one registration per block | Cost spaced by one price half-life | Time between first and last spaced registration |
| --- | ---: | ---: | ---: | ---: |
| 5 | 4 | 0.6393 TAO | 0.4097 TAO | 3.6 hours |
| 20 | 19 | 32.7186 TAO | 1.9097 TAO | 0.9 days |
| 107 | 106 | 8011.9460 TAO | 10.6097 TAO | 5.25 days |

Price movement makes a rapid flood expensive but a patient campaign much
cheaper. Historical registration payments for the observed groups are unknown.
No minimum extra stake, independent GPU, or separate model-training job is
required for each replica by the scoring policy. Forecast computation/data can
be shared; signing, transport, state and lifecycle work grow with hotkeys and
validators. Incremental attacker infrastructure expense is not priced here.

New identities have zero prior EMAs and first five-day resolution around day
six; thirty-day coordinates start around day thirty-one. Early long-horizon
state can lower a blend when it first arrives. In a counterfactual replacing a
current zero-score UID while freezing the other scored cohort, even a perfect
newcomer's first resolved EMA produces a positive **0.02772%** candidate share
but **zero u16 units** at 98% burn. The second resolved round produces two units.
Eligibility is not an immediate promise of emission, and seven-day immunity
leaves limited maturation time under registration competition.

The owner burn reduces the available pool and affects chain quantization. It
does not remove replication advantage within that pool. For five historical
reference copies, earned share is 73.4782%; their pre-encoding share of the whole
vote at 98% burn is about 1.4696%. U16 rounding then changes the normalized
ratio slightly. The emitted vector, Yuma/incentive, and reported Alpha allocation
are separate steps. All-zero scoring uses the owner fallback, not earned
weights. Other chain limits would block scored emission in this release;
experiments refuse to substitute 1/1 if the supplied evidence says otherwise.

`MinerBurned` at the snapshot is approximately 98.2501%, and subnet TAO emission
and TAO pool injection are zero; Alpha-out emission remains 1 Alpha per block.
Therefore zero TAO injection does not imply zero Alpha miner allocation. The
[official emission mechanism](https://www.bittensor.com/docs/concepts/emissions)
also scales inter-subnet demand shares by withheld miner incentive; a burn change
cannot be priced simply as a fixed pool times a smaller miner share.

Conditional thirty-day break-even calculations are supplied in the JSON. In
the historical one-oracle comparison, four simultaneous new clones require a
surviving pool valued above about **0.1001 TAO equivalent per day** to cover the
modeled rapid registration expense, before fees/opex and assuming survival,
frozen competition and constant pool value. This is not a current-mainnet ROI.
The cheaper staggered cost path cannot inherit simultaneous-cohort rewards;
conversion, slippage, reward history and future Alpha prices are unknown.

## Materially different mitigation approaches

The five-copy columns below use the same one-oracle comparisons. All prototypes
are local and use the same attack/control inputs. Numbers are experimental,
not approval thresholds.

| Approach | Five exact copies, old / newer sample | Perturbation or other failure | Honest participation and publication | Cost and rollout risk |
| --- | ---: | --- | --- | --- |
| Current policy | 73.48% / 81.17% | Positive per-hotkey score remains multiplicative | Correct agreement receives full per-hotkey credit; duplicate mass can move median/MAD/tier | Existing deterministic policy; no implementation change |
| Exact forecast budget pooling | 35.65% / 46.30% | Quarter-grace sparse jitter restores 73.45% / 81.17%; arbitrary nonce/hash checks also miss value equality | Correct agreement loses 28.52% / 37.04%; exact consensus domination falls, distinct coordinated forecasts still move C to B | Simple round grouping, but payout/publication semantics and compatibility change |
| Near pooling, tight or wide | 35.65% / 46.30% when copies grouped | Tight 0.01-grace budget misses sparse jitter; wide 0.50-grace budget retains a replica majority inside an honest mixed group | Same correct-agreement losses; split/merge behavior and false merging need further evidence | Complete-link history/scale choices, quadratic prototype work, consensus-critical rollout |
| Excess skill above a causal public baseline | 0% / 55.64% | Any positive excess skill can still be replicated; the newer seed beats the stale baseline | Correct oracle agreements gain here, but all receive zero earned reward in the flat-band coordination control; publication is unchanged | Per-cell baseline provenance/availability, outage handling and versioned payout changes |
| Stronger sharpening, gamma 12 diagnostic | 32.03% / 73.44% | Does not pool identities; enough positive copies still dominate | Raises quality premium but can zero small/newcomer rewards; publication unchanged | Low computation cost, high economic calibration/versioning risk |
| Registration/capital friction | Candidate shares unchanged | Waiting reduces price; coldkey-based restrictions are splittable | Capital/entry/maturation burdens affect honest miners too; consumer protection is indirect | Real chain costs are measurable; changing settings requires separate owner authority/approval |

Removing newcomer publication weight until first resolved evidence would remove
the cold-start floor attack for that interval, but delays honest newcomers about
six days and leaves mature replication intact. Cold start without proven miners
would lack such a feed. This is a publication-only alternative, not a reward
solution, and was not implemented as a production guard.

Allocation-only CPU measurements over sixty cells, minimum of three local
samples: current / exact pooling / wide pooling are **0.067 / 0.156 / 15.969 ms**
for twenty subjects, and **0.697 / 1.444 / 1545.008 ms** for 254 subjects. Public
excess-skill allocation is 0.067 / 0.406 ms. These exclude consensus, parsing,
networking, production overhead and memory; they describe this prototype, not
an optimized rollout bound. Exact matches still trigger near-distance work in
the prototype. Its performance is improvable; correctness and agreement harm
are the more important blockers.

## Proposed acceptance criteria for Apeguru

No approved numeric amplification ceiling, correct-agreement payout-loss limit,
or permitted consumer delta was located. The search covered subnet specs,
economic/release docs, policy/tests/scripts, the three miners' deployment and
evidence docs, and relevant Endure operating decisions. Existing requirements
include duplicate resistance, Alpha-specific economic criteria, independently
operated validators, and a full thirty-day cycle; acceptance of disclosed
limitations does not establish completion. The burn rate and tier cutoffs are
not substitute economic acceptance criteria.

Suggested **approval questions for a next research slice**, not settled policy:

| Measure | Proposed measurable gate |
| --- | --- |
| Replication advantage | Start with a proposed maximum 1.10× aggregate reward amplification for 2/5/20 identities over one, at equal and new-entry histories, then test the observed feasible slot boundary; report both candidates and encoded/Yuma consequences |
| Honest correct agreement | Start with a proposed maximum 5% relative per-operator payout loss in matched independent-agreement controls, and require viable margins under an approved cost envelope; reject a mechanism that merely labels agreement as ownership |
| Consumer distortion | Exact replicas should not add median/tier movement; near replicas within an agreed perturbation/score-loss budget should not create a safer tier through identity count alone; approve native-unit per-output tolerances separately |
| Forecasting premium | A held-out causal improvement must earn a positive, cost-covering premium over the agreed public benchmark; specify the required premium after observing model-cost ranges |
| Evidence coverage | At least seven held-out resolved rounds for initial five-day characterization, plus the first complete thirty-day cycle before claims covering both horizons; missing targets/inputs and peer policy uncertainty remain explicit |
| Operations and rollout | Approve CPU/memory/state budgets from a representative 250-miner, 120-coordinate benchmark; require deterministic replay, causal baselines, archive/absence/UID-change tests, and a version/rollback plan before any production proposal |

The first two targets can conflict for observably identical independent and
Sybil populations. Apeguru must decide what additional evidence or economic
contract would make that tradeoff acceptable. This investigation does not
resolve it by assuming identity proofs, KYC, a similarity threshold, or a cap.

**Bounded follow-up recommendation:** use the existing offline runner to shadow
these gates on a small frozen held-out set when approved, compare a causal
public-skill benchmark with signal-budget alternatives, and stop at the next
decision table. No automated penalty, mainnet monitoring agent, or production
rollout is approved or started by this report.

## Reproduce and inspect

Python 3.12 and the existing locked environment suffice; no dependency or schema
migration was added. These commands read only committed local inputs:

```bash
make dev-install
.venv/bin/python -m scripts.calibrate_alpha_scoring --output /tmp/calibration.json
.venv/bin/python -m scripts.sybil_economics --output /tmp/sybil-results.json
.venv/bin/python -m scripts.sybil_economics --jsonl --output /tmp/sybil-results.jsonl
.venv/bin/python -m scripts.sybil_economics --benchmark --output /tmp/benchmark.json
.venv/bin/python -m pytest tests/scripts/test_sybil_economics.py tests/scripts/test_calibrate_alpha_scoring.py -q
```

The [frozen numerical snapshot](evidence/2026-10-13-public-snapshot.json) records
public raw-response hashes and URLs, schemas, forecasts, outcomes, score blends,
chain/epoch mappings, constraints, and missingness. It normalizes public wire
data rather than retaining secret configuration or a private database. Six
rounds were captured with complete submission paging; the numerical input SHA
is `3f4ee611909012052fd7c2dfc1fc7b219dfc9b824e1292147fa59cd78656e6f3`.
Owner metadata uses `owner_public_account`, verified against the captured
OwnerUID and epoch coldkey vectors.

The [simulation result archive](evidence/2026-10-13-simulation-results.jsonl)
contain 376 baseline/attack/control/policy rows, lifecycle and entry examples,
coordinated counterexamples, price paths, and allocation observations. The
round-tripped full JSON SHA is
`3ecb73b2c08b5cdf05406d5fb30f0d55fd7fa2adba2f026b0c01b5afee7a2f75`.
Each JSONL record has `section` and `data`; experiment records also have a
contiguous zero-based `index`. Collect those records into `experiments`, and
assign each other section its single `data` value to recover the full JSON.
Archive round-trip equality was verified against the runner's canonical JSON.
The [benchmark artifact](evidence/2026-10-13-allocation-benchmark.json) preserves
samples separately because execution timing is nondeterministic.

The [runner](../../scripts/sybil_economics/__main__.py) makes no network request,
opens no wallet/database and contains no production-policy integration. Tests
cover replication math, exact evasion, correct agreement, ownership/order
invariance, causal perturbations, coordinated distortion, horizon maturation,
burn/rounding, entry quantization, pricing and refusal of unsupported chain
constraints and unknown runtime identity. The runner independently computes the
analysis tree's content digest and requires it and the protocol key to match the
captured team-validator identity. This protects the local policy comparison;
it does not attest to peer validators or recover private runtime history.
Verification completed: 1,802 full-suite tests and 18 migration checks passed.
After normalizing public owner-account field names, all 15 investigation and
calibration checks passed again. Final lint, type, domain/protocol/reference/link,
public-release, secret-scanner, and duplication gates passed. The full suite and
migrations were run through `make verify`; the final affected tests and remaining
gates were rerun with the normalized inputs.

Relevant production definitions remain in [reward math](../../endure/scoring/weights.py),
[publication math](../../endure/aggregation/consensus.py),
[burn allocation](../../endure/scoring/emission_policy.py),
[encoding](../../endure/scoring/weight_processing.py),
[tier derivation](../../endure/publication/risk_tier.py), and
[economic limitations](../economic-limitations.md).
