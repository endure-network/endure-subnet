# Alpha Risk V1 — scope (2026-07-06)

> **Status: current.** Canonical product scope — the served vertical.

> **KRE status:** the former bank-risk vertical, runtime, fixtures, and five
> dedicated tables have been removed. KRE references below describe historical
> design lineage only and are not implementation or operator guidance.

Endure's first served vertical pivots from Forge lending parameters to
**general risk assessment of Bittensor subnet Alpha tokens**: miners predict
objectively resolvable risk observables per whitelisted netuid, validators
resolve realized values from recorded market data and score accuracy, and
consumers read a per-subnet risk feed (including a derived A–E risk tier).

Schema ID: **`risk.v1.subnet_alpha`**. Supersedes
`docs/specs/2026-06-19-forge-lending-v1-stage1-scope.md` as the activation
target; the Forge lending schema goes dormant in-tree (see §Handling Forge
lending). Its former activation plan is complete through Batch 7; the remaining
Batches 8–9 are replaced by the build sequence below.

## Why this pivot

- **No external blocker.** Forge lending's serving flip was gated on Forge's
  confirmed parameter ranges — an input outside our control. Every scoring
  constant in this schema is ours to set and tune before the flip.
- **No placeholder outputs.** The three 10^30 placeholder ceilings
  (safe_asset_price, supply_cap, borrow_cap) existed because those outputs
  needed Forge market context. Here every wire field is scored against an
  objective realized target; nothing ships unscored.
- **Forge lending layers on top later.** The four scored outputs are the risk
  primitives Forge's seven parameters derive from: max_drawdown →
  collateral_factor, realized_volatility → liquidation_threshold/incentive,
  twap_price → safe_asset_price, liquidity_depth → supply_cap/borrow_cap.
  Lending V2 becomes a consumer/derivation of risk consensus.
- **~80% of the built machinery carries over unchanged**: commit/reveal round
  engine, coordinate spine and generic assessment storage, weighted-median
  consensus, bounded-linear deviation scoring, EMA/weights, universe
  whitelist gate, Alpha market-data layer.

## Locked V1 decisions

1. Four scored, miner-submitted outputs per netuid (table below); a derived
   risk tier at the publication layer; no unscored miner fields anywhere.
2. Two horizons per output — 5 days (432000 s) and 30 days (2592000 s) —
   both scored from day one; per-horizon resolution passes.
3. Daily uniform commit/reveal rounds on the calendar-independent fixed-UTC
   schedule; offsets remain unchanged. See
   `2026-07-18-alpha-risk-24x7-rounds.md` for the 20:00 UTC anchor and 7/7
   collection contract.
4. Static whitelist universe via the existing lending-universe gate,
   versioned in code, snapshotted into each round. Dynamic selection deferred.
5. Build strategy: generalize the scoring engine first, then register the
   risk schema as a thin config. Forge lending stays dormant in-tree as a
   second thin config.
6. Stage-1 remains sim-free and mainnet-data-on-testnet, per the Forge
   Stage-1 decisions.
7. Decimal everywhere internally; wire values are strict integers.
8. A reference test miner ships with the schema, and a full validator↔miner
    cycle on local devnet is an explicit pre-flip acceptance milestone (R5),
    run under compressed schedules that require mock/local endpoints or an
    explicit Bittensor testnet serving-stage acknowledgement; mainnet
    compression always hard-fails.

## V1 scored schema (`risk.v1.subnet_alpha`)

Serving status `registered_unserved` from first commit; flipped to `served`
in batch R6 (the semantic activation — protocol version key bump happens
there and only there).

Wire bundle mirrors the lending envelope: signed canonical bundle of
`assets[]`, each asset carrying `outputs[]` of
`(output, value: StrictInt, confidence_bps, reason_codes, horizon_seconds,
unit)`. Four outputs × two horizons = 8 values per netuid per round.
`confidence_bps` and `reason_codes` are carried but not scored in V1.

| Output | Prediction over the horizon window | Wire unit |
|---|---|---|
| `max_drawdown` | Worst peak-to-trough price decline | bps |
| `realized_volatility` | Annualized volatility of log returns | bps |
| `twap_price` | Time-weighted average Alpha price | RAO per Alpha (TAO × 10⁹) |
| `liquidity_depth` | Time-weighted average TAO reserve of the subnet pool | RAO |

`max_drawdown` is standard peak-to-trough — a new observable, distinct from
the lending CF observable's drawdown-from-entry, which stays untouched for
the dormant lending path.

### Realized-value estimators

All estimators read the recorded snapshot series (block-stamped) inside the
horizon window `(reveal_close, reveal_close + horizon]`:

- **max_drawdown**: `max over snapshot pairs t1 ≤ t2 of (1 − p(t2)/p(t1))`,
  floored at 0, in bps of the peak price.
- **realized_volatility**: population standard deviation of log returns
  between consecutive snapshots, annualized via the canonical snapshot
  cadence (`sqrt(periods_per_year)`), expressed in bps (e.g. 80% annualized
  = 8000).
- **twap_price**: block-weighted mean of price over the window (each
  snapshot weighted by the block interval it covers), in RAO per Alpha.
- **liquidity_depth**: same block-weighted mean over the pool's TAO reserve,
  in RAO.

**Voiding**: a (netuid, horizon) cell is voided — realized value `None`,
nobody scored on it — when the window holds fewer than 20 snapshots or the
first-to-last snapshot span covers less than 80% of the horizon. Voiding a
30d cell does not void the 5d cell or vice versa.

**Per-horizon live resolution windows:** live resolution windows are constructed
per `(round, netuid, horizon)`; a 5d pass fetches only the 5d block window,
never the 30d window. Archive series are gap-tolerant: individual missing
canonical-cadence blocks are skipped and the voiding rule above decides whether
the remaining series is usable. `realized_volatility` uses only adjacent
snapshot pairs separated by exactly the canonical cadence; pairs that span a gap
are ignored rather than annualized as one-cadence returns.

**Sparse-window estimator guards:** `realized_volatility` requires at least 10
exact-cadence adjacent returns (half the minimum snapshot count); otherwise the
coordinate is voided instead of resolving as a zero-risk knife edge. Backward
block weights for `twap_price` and `liquidity_depth` are capped at one canonical
cadence per snapshot, so leading gaps are unrepresented rather than wholly
attributed to the first observed value.

### Scoring

Bounded-linear asymmetric deviation, the CF scoring family: score 1 inside
the grace band, falling linearly to 0 at the cutoff; the risk-understating
(aggressive) direction uses the listed cutoff, the conservative (lenient)
direction stretches it 3× (`lenient_multiplier = 3`).

Absence-aware scoring (key 21): the scoring set, zero-fill for absent
miners, deregistration archival, and epsilon pruning are specified in
`docs/specs/2026-07-20-scoring-fairness-deltas.md` §1.

| Output | Aggressive direction | Deviation mode | Grace | Cutoff |
|---|---|---|---|---|
| `max_drawdown` | too low | absolute bps | 200 | 2000 |
| `realized_volatility` | too low | absolute bps | 500 | 5000 |
| `twap_price` | too high | relative (bps of realized) | 200 (2%) | 2000 (20%) |
| `liquidity_depth` | too high | relative (bps of realized) | 500 (5%) | 5000 (50%) |

Relative mode scores the deviation as a fraction of the realized value, so
one spec covers netuids whose price and depth differ by orders of magnitude.
All grace/cutoff/multiplier constants are tunable until the R6 serving flip
and frozen at it.

**Coverage penalty**: a resolved coordinate the miner's accepted bundle
skipped scores 0, so cherry-picking easy assets cannot beat full coverage.

**EMA and weights**: one EMA per (hotkey, netuid, output, horizon)
coordinate on the existing spine; blended score = equal-weight mean of a
miner's scored-coordinate EMAs; weights via the existing sharpened
normalization with all-zero → abstain. Output/horizon weighting in the blend
is deferred.

### Round lifecycle and per-horizon resolution

Rounds keep `open → revealed → closed`. On each validator tick, every
(round, horizon) past `reveal_close + horizon` with no realized targets yet
gets one atomic, structurally idempotent scoring pass (the Batch 7B
`record_assessment_scoring_pass`, keyed per horizon). The round closes when
all horizons are resolved. Zero-submission rounds still write realized
targets per horizon as completion markers and close normally. Steady state
holds ~30 unresolved rounds; first 5d scores arrive at launch + 5 days,
first 30d scores at launch + 30 days (5d carries the incentive signal until
then).

### Consensus

Per-coordinate weighted median + MAD over accepted reveals, blend-weighted
with the existing minimum consensus weight — the generic consensus shipped
in Batch 7A, renamed schema-agnostic. Published atomically with the reveal
flip as today.

### Derived risk tier (publication layer, not on the wire)

Validators derive a per-netuid tier from the **30d consensus medians** of
`max_drawdown` (dd) and `realized_volatility` (v); the worse dimension wins:

| Tier | Condition (both must hold) |
|---|---|
| A | dd < 1000 and v < 5000 |
| B | dd < 2000 and v < 8000 |
| C | dd < 3500 and v < 12000 |
| D | dd < 5000 and v < 16000 |
| E | otherwise |

Voided or not-yet-resolved 30d data → `unrated`. Thresholds are tunable
until the R6 flip, frozen at it, and recalibrated only via a versioned spec
change.

**Consensus freshness and tier provenance:** the published risk feed derives the
tier from an older round when needed, while the per-output consensus block
remains the newest post-embargo consensus round. Consumers get fresh predictions
while the tier honestly lags until 30d data resolves.

**Per-netuid tier provenance:** tier provenance is per subnet: each feed subnet
derives its tier from the newest post-embargo round where that netuid has both
30d tier-bearing coordinates resolved with non-null values and both matching 30d
consensus rows present. The subnet entry exposes `tier_round_id` / `tier_as_of`;
top-level tier provenance fields from the 2026-07-07 feed draft are removed. The
top-level consensus `round_id` / `as_of` remain the newest post-embargo
consensus block. The signed feed payload includes `feed_schema_version: 1` for
future consumer-visible shape changes.

**Whitelist-filtered publication:** the feed subnet list is filtered by the
current Alpha Risk whitelist, then sorted from the union of the newest consensus
universe and whitelisted netuids with tier-bearing history. A de-whitelisted
netuid is omitted even if older rounds contain consensus or tier-bearing history;
this prevents validators from continuing to serve risk signals for delisted or
suspect assets. Frozen thresholds are unchanged.

## Universe

The existing static-whitelist gate reused as-is: a curated netuid list
versioned in code, snapshotted into each round's stored universe. Launch
list: 10–20 subnets with reliable pool data, selected at R2 by liquidity
depth and data availability from the recorded series; grown by PR
thereafter.

## Market-data extension

Snapshots gain the pool's TAO reserve alongside the price already derived
from it. The provider payload hash covers reserves; the recorded mainnet
fixtures are re-cut with reserves at the canonical cadence. This is the only
net-new ingestion and is what makes `liquidity_depth` resolvable. The
reference miner ships the same provider interface.

## Reference test miner

A runnable reference miner ships with the schema (assembler in R2, neuron
wiring in R5), serving two purposes: the devnet acceptance milestone below,
and the public starting point third-party miners fork.

- **Baseline assembler**: constants for the bps outputs — 5d: drawdown
  1500, vol 8000; 30d: drawdown 3000, vol 8000 — and persistence (latest
  observed value) for `twap_price` and `liquidity_depth` from the miner's
  provider.
- **Wiring**: `neurons/miner.py` assembles, commits, and reveals
  `risk.v1.subnet_alpha` bundles on the round schedule via the existing
  miner round service — full commit/reveal transport, not a test stub.
- **Runnable in both dev modes**: mock mode (no chain) and local devnet
  (registered hotkey on a local subtensor), per `docs/running_locally.md`.

## Devnet full-cycle milestone (R5)

Before the serving flip, one complete validator↔miner cycle must run on a
local devnet chain — real wallets, real commit/reveal transport, real
weights extrinsic — proving every seam end to end. Acceptance criteria, all
in a single documented run:

1. Validator opens a `risk.v1.subnet_alpha` round; the reference miner
   commits and reveals through the axon transport and the bundle is
   accepted.
2. Reveal closes; consensus rows publish for every (netuid, output,
   horizon) coordinate in the whitelist.
3. Both resolution passes fire under the compressed schedule, writing
   realized targets from the recorded mainnet fixtures (5d pass, then 30d
   pass), and the round closes.
4. The miner's EMAs and blended score are positive, and the validator
   submits a non-zero weights extrinsic to the local chain.
5. The run is reproducible from a one-command entry point (make target
   plus a `docs/running_locally.md` section), and the same cycle runs in
   mock mode as an automated integration test in CI.

**Dev-only time compression**: round offsets and both horizons are
compressible via dev configuration (seconds instead of days) so the full
cycle completes in one session. Compression is refused unless the
configured chain is mock/local, or Bittensor testnet with the explicit
`--endure.serving_stage testnet` acknowledgement for chain-integration
shakedowns; mainnet compression is always refused by code. Scoring constants
are untouched by compression; only the schedule shrinks.

## Reused vs. net-new

Reused unchanged: round engine and windows, commit/reveal + canonical
bundles, coordinate spine and all `assessment_*` storage, atomic scoring
pass, deviation/EMA/weights math, universe gate, market-data provider
seam, health/read plumbing.

Generalized (R1): scoring orchestrator (per-schema resolver table:
`output → (resolve fn, scoring spec)`), per-horizon resolution scheduler,
consensus function renamed schema-agnostic. Lending tests stay green
throughout — they are the regression harness for the spine.

Net-new (R2–R5): risk schema module (outputs, wire bundle, scoring specs,
baseline assembler), two observables (peak-to-trough drawdown, block-weighted
mean) plus TWAP/volatility estimators, reserve-bearing snapshots and
fixtures, risk read API/publication with tier derivation, neurons wiring and
the devnet full-cycle harness (schedule compression + guard, make target,
CI mock cycle).

## Build sequence

Batches are delivered in order, each fully tested before the next.

| Batch | Content |
|---|---|
| R0 | This scope doc (the pivot builds on Batch 7B's spine); target-vertical update in repository guidance; dormancy note in the Forge scope doc |
| R1 | Generalize orchestrator + per-horizon scheduler + consensus rename; no new schema; lending tests prove no regression |
| R2 | `risk.v1.subnet_alpha` schema module, wire bundle, scoring specs, baseline assembler, launch whitelist — `registered_unserved` |
| R3 | Reserve-bearing snapshots, re-cut mainnet fixtures, the four estimators with golden-value tests |
| R4 | Read API + publication for risk consensus + tier derivation |
| R5 | Devnet full-cycle milestone: neurons wiring (miner + validator), compressed schedule guard for mock/local plus explicitly acknowledged testnet shakedowns, mock-mode cycle as CI integration test, documented localnet run meeting the acceptance criteria above |
| R6 | Serving flip: `served`, protocol key bump, live Alpha price/reserve provider, freeze scoring constants and tier table |
| R7 | Testnet soak — wall-clock gate before any mainnet deploy (unchanged hard requirement) |

## Protocol version / digest

All R0–R5 changes touch watched paths only in `registered_unserved` state →
digest re-folds at the current key (devnet runs exercise the schema without
changing its production serving status). The R6 serving flip is the semantic
activation → single key bump there. No key bump before R6.

## Handling Forge lending (transition)

`lending.v1.subnet_asset` stays **dormant in-tree**: schema registered
`registered_unserved`, with its code and tests retained. The R1 generalization
turns its orchestrator into one resolver-table
config rather than deleting or duplicating it. Reactivation path: Forge V2
derives lending parameters from risk consensus once Forge's parameter ranges
arrive. The Forge scope doc carries the dormancy contract; unqualified "spec §"
citations in lending code continue
to resolve against the Forge scope doc.

## Explicitly deferred

- Dynamic universe selection (top-N by liquidity).
- Slippage-at-size output (derivable from `liquidity_depth` under the AMM
  curve).
- Output/horizon weighting in the blended score; confidence-weighted
  scoring.
- Forge lending reactivation and any lending parameter derivation.
- Simulation-based scoring; any non-mainnet data source.

## Open questions / risks

- **Live reserve history**: the R6 live provider must read TAO reserves at
  past blocks; confirm archive-node access before R6 (recorded fixtures
  carry R2–R5, including the devnet cycle).
- **Volatility cadence sensitivity**: irregular snapshot gaps bias the
  annualized estimator; the block-weighted estimators and the 80%-coverage
  voiding rule bound this, but R3's golden tests must include a gappy
  series.
- **Tier calibration**: the A–E thresholds are informed guesses until soak
  data exists; R7 explicitly reviews tier distribution across the whitelist
  before freeze is considered final.
- **Miner cold start**: no 30d signal until launch + 30 days; acceptable
  because 5d scores drive weights from day 5.
